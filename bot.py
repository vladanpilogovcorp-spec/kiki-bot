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
- VIP-группа (высший приоритет): 1, 2, 3, 4, 333, 555, 777
- Дополнительные: VIP1, VIP2, VIP3, VIP4, 11-25, 31-33, 41-43, 51, 52, wc1, wc2

ЗАЯВКА:
Стол → Вынос/ДР → (если ДР: текст / текст+алкоголь / текст+алкоголь+трек) →
алкоголь (если нужен) → текст поздравления → трек да/нет (Натали) →
фото или текст к заявке → кальян да/нет (если да — меню кальянов) →
отметка «чек 200+» → подтверждение → отправка.

Если в заявке есть кальян, она отдельно прилетает кальянщикам. Заявка
становится по-настоящему «готовой» только когда официант (и кальянщик, если
он есть в заявке) оба нажали «Готов».

Приоритет считается автоматически: VIP-стол, отметка «чек 200+», ДР и
необходимость трека поднимают заявку выше в очереди. На экранах очереди
бот показывает рекомендацию следующего выноса.

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
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    MenuButtonCommands,
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

VIP_TABLES = ["1", "2", "3", "4", "333", "555", "777"]
REGULAR_TABLES = [
    "VIP1", "VIP2", "VIP3", "VIP4",
    "11", "12", "13", "14", "15", "16", "17", "18", "19", "20",
    "21", "22", "23", "24", "25", "31", "32", "33", "41", "42", "43",
    "51", "52", "wc1", "wc2",
]
ALL_TABLES = VIP_TABLES + [t for t in REGULAR_TABLES if t not in VIP_TABLES]

ALCOHOL_OPTIONS = ["Азуль", "Дон Периньон", "Дон Хулио", "Белуга 6л", "Кристалл"]
HOOKAH_MENU = ["Кальян бутылка", "Кальян лакики бу", "Кальян Пайпай", "Кальян доби дог", "Кальян Арман 12 л", "Кальян флеш"]

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
    "dancer": "Танцовщица",
    "music": "Музыка (Натали)",
    "admin": "Админ",
}

# Коды для входа в роль — задаются на Railway переменной ROLE_CODES вида
# "waiter=1111,hookah=2222,hookah_chief=2223,mc=3333,dancer=4444,music=5555,admin=9999"
# Без этой переменной используются коды по умолчанию ниже (смени их!).
_DEFAULT_ROLE_CODES = "waiter=1111,hookah=2222,hookah_chief=2223,mc=3333,dancer=4444,music=5555,admin=9999"
ROLE_CODES: dict[str, str] = {}
for pair in os.environ.get("ROLE_CODES", _DEFAULT_ROLE_CODES).split(","):
    if "=" in pair:
        role_key, code = pair.split("=", 1)
        ROLE_CODES[code.strip()] = role_key.strip()

user_roles: dict[int, str] = {}
# по умолчанию первый, кто напишет /start и введёт код админа, становится
# админом; дальше админ может назначать роли другим через /setrole
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
    announce_text: Optional[str] = None  # текст для МС в заявках кальянщика
    track: bool = False
    note_text: Optional[str] = None
    note_photo_id: Optional[str] = None
    hookah: bool = False
    hookah_item: Optional[str] = None
    big_check: bool = False

    no_waiter: bool = False  # заявка подана напрямую кальянщиком, без официанта
    dancer: bool = False  # участвуют танцовщицы, нужно их подтверждение

    status: str = "collecting"  # collecting -> ready -> announced -> done
    ready_waiter: bool = False
    ready_hookah: bool = False
    ready_track: bool = False
    ready_mc: bool = False
    ready_dancer: bool = False

    waiter_name: str = ""
    hookah_name: str = ""
    music_name: str = ""

    created_at: datetime = field(default_factory=lambda: datetime.now(TIMEZONE))
    ready_at: Optional[datetime] = None
    announced_at: Optional[datetime] = None
    done_at: Optional[datetime] = None

    def needs_hookah_confirm(self) -> bool:
        return self.hookah

    def needs_track_confirm(self) -> bool:
        return self.track

    def is_fully_ready(self) -> bool:
        if self.needs_hookah_confirm() and not self.ready_hookah:
            return False
        if self.needs_track_confirm() and not self.ready_track:
            return False
        if self.no_waiter:
            if not self.ready_mc:
                return False
            if self.dancer and not self.ready_dancer:
                return False
            return True
        return self.ready_waiter

    def priority_score(self) -> tuple:
        # Меньше = выше приоритет
        vip_rank = 0 if self.is_vip_table else 1
        check_rank = 0 if self.big_check else 1
        bday_rank = 0 if self.order_type == "bday" else 1
        show_rank = 0 if self.track else 1
        ref_time = self.ready_at or self.created_at
        return (vip_rank, check_rank, bday_rank, show_rank, ref_time)


class ChatState:
    def __init__(self) -> None:
        self.orders: dict[int, Order] = {}
        self.next_id = 1
        self.paused = False
        self.current_show: Optional[str] = None
        self.show_program: list[dict] = []
        self.current_show_index: Optional[int] = None
        self.program_message_id: Optional[int] = None
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
        rows.append(["📋 Просмотреть заявки"])
    if role in ("hookah", "hookah_chief", "admin"):
        rows.append(["📋 Новая заявка (свой вынос)"])
        rows.append(["💨 Заявки на кальян"])
    if role in ("hookah_chief", "admin"):
        rows.append(["👑 Контроль кальянщиков"])
    if role in ("mc", "admin"):
        rows.append(["🎤 Экран MC"])
    if role in ("music", "admin"):
        rows.append(["🎵 Треки"])
    if role in ("dancer", "admin"):
        rows.append(["💃 Мои выносы"])
    if role in ("mc", "music", "dancer"):
        rows.append(["🚻 Отошёл"])
    if role == "admin":
        rows.append(["🧑‍💼 Менеджер", "🎬 Программа"])
        rows.append(["📊 Статистика", "👀 Всё сразу"])

    if not rows:
        rows = [["ℹ️ Кто я?"]]

    rows.append(["🔄 Сменить роль"])

    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows], resize_keyboard=True)


class RolePick(StatesGroup):
    entering_code = State()


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    role = get_role(message.from_user.id)
    if role:
        await message.answer(f"Ты вошёл как: {ROLE_LABELS[role]}", reply_markup=role_keyboard(message.from_user.id))
    else:
        await state.set_state(RolePick.entering_code)
        prompt = await message.answer("Введи код своей роли (его даёт админ):")
        await state.update_data(prompt_id=prompt.message_id)


@router.message(F.text == "🔄 Сменить роль")
async def switch_role(message: Message, state: FSMContext) -> None:
    await state.set_state(RolePick.entering_code)
    prompt = await message.answer("Введи код роли, на которую хочешь переключиться:")
    await state.update_data(prompt_id=prompt.message_id)


@router.message(RolePick.entering_code)
async def enter_role_code(message: Message, state: FSMContext) -> None:
    code = message.text.strip()
    role = ROLE_CODES.get(code)
    data = await state.get_data()
    prompt_id = data.get("prompt_id")
    await try_delete(message)
    if not role:
        await message.answer("Код не найден. Попробуй ещё раз или уточни у админа.")
        return
    user_roles[message.from_user.id] = role
    await state.clear()
    if prompt_id:
        try:
            await message.bot.delete_message(message.chat.id, prompt_id)
        except TelegramBadRequest:
            pass
    await message.answer(f"Готово, ты — {ROLE_LABELS[role]}", reply_markup=role_keyboard(message.from_user.id))


@router.message(F.text == "🚻 Отошёл")
async def btn_away(message: Message) -> None:
    role = get_role(message.from_user.id)
    if role not in ("mc", "music", "dancer"):
        return
    await message.answer(
        f"⚠️ {ROLE_LABELS[role]} ({message.from_user.full_name}) ненадолго отошёл(-ла). "
        f"Учтите задержку по своей части."
    )


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
    asking_hookah = State()
    choosing_hookah_item = State()
    asking_big_check = State()
    asking_note_yn = State()
    entering_note = State()
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


def alcohol_keyboard(allow_none: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=a, callback_data=f"alcohol:{a}")] for a in ALCOHOL_OPTIONS]
    if allow_none:
        rows.append([InlineKeyboardButton(text="Без алкоголя", callback_data="alcohol:none")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hookah_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=h, callback_data=f"hookahitem:{h}")] for h in HOOKAH_MENU]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Всё верно, отправить", callback_data="confirm:send")],
        [InlineKeyboardButton(text="✏️ Редактировать пункт", callback_data="confirm:edit")],
        [InlineKeyboardButton(text="✏️ Начать заново", callback_data="confirm:restart")],
    ])


def edit_order_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стол", callback_data="edit_field:table"),
         InlineKeyboardButton(text="Тип / ДР", callback_data="edit_field:type")],
        [InlineKeyboardButton(text="Алкоголь", callback_data="edit_field:alcohol"),
         InlineKeyboardButton(text="Текст", callback_data="edit_field:congrats")],
        [InlineKeyboardButton(text="Трек", callback_data="edit_field:track"),
         InlineKeyboardButton(text="Кальян", callback_data="edit_field:hookah")],
        [InlineKeyboardButton(text="200+", callback_data="edit_field:bigcheck"),
         InlineKeyboardButton(text="Фото / комментарий", callback_data="edit_field:note")],
        [InlineKeyboardButton(text="↩️ Вернуться к проверке", callback_data="edit_field:back")],
    ])


@router.message(F.text == "📋 Новая заявка")
async def start_new_order(message: Message, state: FSMContext) -> None:
    if get_role(message.from_user.id) not in ("waiter", "admin"):
        await message.reply("Заявки подают официанты.")
        return
    await state.clear()
    await state.set_state(NewOrder.choosing_table)
    await message.answer("Выбери стол:", reply_markup=tables_keyboard())


@router.message(F.text == "📋 Новая заявка (свой вынос)")
async def start_new_order_hookah(message: Message, state: FSMContext) -> None:
    if get_role(message.from_user.id) not in ("hookah", "hookah_chief", "admin"):
        return
    await start_hookah_own_order(message, state)


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
        await callback.message.edit_text("Выбери бутылку:", reply_markup=alcohol_keyboard(allow_none=True))
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


@router.callback_query(NewOrder.choosing_alcohol, F.data.startswith("alcohol:"))
async def pick_alcohol(callback: CallbackQuery, state: FSMContext) -> None:
    alcohol = callback.data.split(":", 1)[1]
    await state.update_data(alcohol=None if alcohol == "none" else alcohol)
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
        await ask_hookah(message, state)
    else:
        await state.set_state(NewOrder.asking_track)
        await message.answer("Нужен трек? (тегнем Натали)", reply_markup=yes_no_keyboard("track"))


@router.callback_query(NewOrder.asking_track, F.data.startswith("track:"))
async def pick_track(callback: CallbackQuery, state: FSMContext) -> None:
    track = callback.data.split(":", 1)[1] == "yes"
    await state.update_data(track=track)
    await ask_hookah(callback.message, state, edit=True)
    await callback.answer()


async def ask_hookah(message: Message, state: FSMContext, edit: bool = False) -> None:
    await state.set_state(NewOrder.asking_hookah)
    text = "Добавка кальяном?"
    kb = yes_no_keyboard("hookah")
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@router.callback_query(NewOrder.asking_hookah, F.data.startswith("hookah:"))
async def pick_hookah(callback: CallbackQuery, state: FSMContext) -> None:
    wants_hookah = callback.data.split(":", 1)[1] == "yes"
    await state.update_data(hookah=wants_hookah)
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
    await state.set_state(NewOrder.asking_note_yn)
    await callback.message.edit_text(
        "Добавить фото или текст к заявке? (необязательно)",
        reply_markup=yes_no_keyboard("addnote"),
    )
    await callback.answer()


@router.callback_query(NewOrder.asking_note_yn, F.data.startswith("addnote:"))
async def pick_add_note(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data.split(":", 1)[1] == "no":
        await go_to_confirm(callback.message, state, edit=True)
    else:
        await state.set_state(NewOrder.entering_note)
        await callback.message.edit_text("Пришли фото из галереи или напиши текст.")
    await callback.answer()


@router.message(NewOrder.entering_note, F.photo)
async def enter_note_photo(message: Message, state: FSMContext) -> None:
    await state.update_data(note_photo_id=message.photo[-1].file_id, note_text=message.caption)
    await try_delete(message)
    await go_to_confirm(message, state)


@router.message(NewOrder.entering_note)
async def enter_note_text(message: Message, state: FSMContext) -> None:
    await state.update_data(note_text=message.text.strip())
    await try_delete(message)
    await go_to_confirm(message, state)


async def go_to_confirm(message: Message, state: FSMContext, edit: bool = False) -> None:
    await state.set_state(NewOrder.confirming)
    data = await state.get_data()
    summary = build_summary(data)
    if edit:
        await message.edit_text(summary, reply_markup=confirm_keyboard(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(summary, reply_markup=confirm_keyboard(), parse_mode=ParseMode.HTML)


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
    lines.append(f"Кальян: {'да (вкус выберет кальянщик)' if data.get('hookah') else 'нет'}")
    lines.append(f"Чек 200+: {'да' if data.get('big_check') else 'нет'}")
    return "\n".join(lines)


@router.callback_query(NewOrder.confirming, F.data == "confirm:edit")
async def open_order_editor(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "Что именно изменить? Остальные данные заявки сохранятся:",
        reply_markup=edit_order_keyboard(),
    )
    await callback.answer()


@router.callback_query(NewOrder.confirming, F.data.startswith("edit_field:"))
async def edit_order_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1]
    if field == "back":
        await go_to_confirm(callback.message, state, edit=True)
    elif field == "table":
        await state.set_state(NewOrder.choosing_table)
        await callback.message.edit_text("Выбери новый стол:", reply_markup=tables_keyboard())
    elif field == "type":
        await state.set_state(NewOrder.choosing_type)
        await callback.message.edit_text("Выбери новый тип заявки:", reply_markup=type_keyboard())
    elif field == "alcohol":
        await state.set_state(NewOrder.choosing_alcohol)
        await callback.message.edit_text("Выбери алкоголь:", reply_markup=alcohol_keyboard(allow_none=True))
    elif field == "congrats":
        await state.set_state(NewOrder.entering_congrats)
        await callback.message.edit_text("Напиши новый текст поздравления:")
    elif field == "track":
        await state.set_state(NewOrder.asking_track)
        await callback.message.edit_text("Нужен трек?", reply_markup=yes_no_keyboard("track"))
    elif field == "hookah":
        await state.set_state(NewOrder.asking_hookah)
        await callback.message.edit_text("Добавить кальян?", reply_markup=yes_no_keyboard("hookah"))
    elif field == "bigcheck":
        await state.set_state(NewOrder.asking_big_check)
        await callback.message.edit_text("Чек гостя 200+?", reply_markup=yes_no_keyboard("bigcheck"))
    elif field == "note":
        await state.set_state(NewOrder.entering_note)
        await callback.message.edit_text("Пришли новое фото или напиши новый комментарий:")
    await callback.answer()


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


# ---------------------------------------------------------------------------
# СОБСТВЕННЫЙ ВЫНОС КАЛЬЯНЩИКА (без официанта)
# ---------------------------------------------------------------------------


class HookahOwnOrder(StatesGroup):
    choosing_table = State()
    choosing_item = State()
    asking_track = State()
    asking_announce = State()
    entering_announce = State()
    confirming = State()


def hookah_own_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Всё верно, отправить", callback_data="hown_confirm:send")],
        [InlineKeyboardButton(text="✏️ Начать заново", callback_data="hown_confirm:restart")],
    ])


async def start_hookah_own_order(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(HookahOwnOrder.choosing_table)
    await message.answer("Свой вынос с кальяном. Выбери стол:", reply_markup=tables_keyboard())


@router.callback_query(HookahOwnOrder.choosing_table, F.data.startswith("table:"))
async def hown_pick_table(callback: CallbackQuery, state: FSMContext) -> None:
    table = callback.data.split(":", 1)[1]
    await state.update_data(table=table, is_vip_table=table in VIP_TABLES)
    await state.set_state(HookahOwnOrder.choosing_item)
    await callback.message.edit_text(f"Стол {table}. Какой кальян?", reply_markup=hookah_menu_keyboard())
    await callback.answer()


@router.callback_query(HookahOwnOrder.choosing_item, F.data.startswith("hookahitem:"))
async def hown_pick_item(callback: CallbackQuery, state: FSMContext) -> None:
    item = callback.data.split(":", 1)[1]
    await state.update_data(hookah_item=item)
    await state.set_state(HookahOwnOrder.asking_track)
    await callback.message.edit_text("Нужен трек? (тегнем Натали)", reply_markup=yes_no_keyboard("hown_track"))
    await callback.answer()


@router.callback_query(HookahOwnOrder.asking_track, F.data.startswith("hown_track:"))
async def hown_pick_track(callback: CallbackQuery, state: FSMContext) -> None:
    track = callback.data.split(":", 1)[1] == "yes"
    await state.update_data(track=track)
    await state.set_state(HookahOwnOrder.asking_announce)
    await callback.message.edit_text("Нужен текст для МС — что сказать?", reply_markup=yes_no_keyboard("hown_announce"))
    await callback.answer()


@router.callback_query(HookahOwnOrder.asking_announce, F.data.startswith("hown_announce:"))
async def hown_pick_announce(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data.split(":", 1)[1] == "no":
        await state.update_data(announce_text=None)
        await hown_go_to_confirm(callback.message, state, edit=True)
    else:
        await state.set_state(HookahOwnOrder.entering_announce)
        await callback.message.edit_text("Напиши текст, что сказать МС.")
    await callback.answer()


@router.message(HookahOwnOrder.entering_announce)
async def hown_enter_announce(message: Message, state: FSMContext) -> None:
    await state.update_data(announce_text=message.text.strip())
    await try_delete(message)
    await hown_go_to_confirm(message, state)


async def hown_go_to_confirm(message: Message, state: FSMContext, edit: bool = False) -> None:
    await state.set_state(HookahOwnOrder.confirming)
    data = await state.get_data()
    lines = ["<b>Проверь заявку:</b>", ""]
    lines.append(f"Стол: {data.get('table')}")
    lines.append(f"Кальян: {data.get('hookah_item')}")
    lines.append(f"Трек: {'да' if data.get('track') else 'нет'}")
    if data.get("announce_text"):
        lines.append(f"Текст для МС: {data['announce_text']}")
    text = "\n".join(lines)
    if edit:
        await message.edit_text(text, reply_markup=hookah_own_confirm_keyboard(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(text, reply_markup=hookah_own_confirm_keyboard(), parse_mode=ParseMode.HTML)


@router.callback_query(HookahOwnOrder.confirming, F.data == "hown_confirm:restart")
async def hown_restart(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(HookahOwnOrder.choosing_table)
    await callback.message.edit_text("Начинаем заново. Выбери стол:", reply_markup=tables_keyboard())
    await callback.answer()


@router.callback_query(HookahOwnOrder.confirming, F.data == "hown_confirm:send")
async def hown_send(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    chat_id = callback.message.chat.id
    chat_state = get_state(chat_id)

    order = chat_state.add_order(
        table=data["table"],
        is_vip_table=data.get("is_vip_table", False),
        order_type="vynos",
        track=data.get("track", False),
        hookah=True,
        hookah_item=data.get("hookah_item"),
        announce_text=data.get("announce_text"),
        no_waiter=True,
        dancer=True,
        hookah_name=callback.from_user.full_name,
    )

    await state.clear()
    await callback.message.edit_text(f"✅ Свой вынос по столу {order.table} отправлен на подтверждение.")
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
        track_status = "🟢 трек готов" if o.ready_track else "🟡 трек готовится"
        tags.append(f"{track_status} (cc @{MC_USERNAME} @{MUSIC_USERNAME})")
    tag_str = f" ({', '.join(tags)})" if tags else ""
    status_label = {
        "collecting": "🟡 Собирается",
        "ready": "🟢 Готово",
        "announced": "🟣 Объявлено",
    }.get(o.status, o.status)
    return f"{mark} Стол {o.table} — {kind}{tag_str} — {status_label}"


def recommendation_text(chat_state: ChatState) -> str:
    if chat_state.paused:
        return "\n⏸ <b>Рекомендация:</b> очередь приостановлена из-за текущего номера."
    active = chat_state.active_orders()
    if not active:
        return "\n💡 <b>Рекомендация:</b> активных заявок нет."
    ready = [o for o in active if o.status == "ready"]
    order = ready[0] if ready else active[0]
    reason = []
    if order.is_vip_table:
        reason.append("VIP-стол")
    if order.big_check:
        reason.append("чек 200+")
    if order.order_type == "bday":
        reason.append("ДР")
    if order.track:
        reason.append("нужен трек")
    why = ", ".join(reason) if reason else "ранняя заявка в очереди"
    return f"\n⭐ <b>Рекомендуемый следующий вынос:</b> стол {order.table} ({why})."


def render_waiter(chat_state: ChatState, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    orders = [o for o in chat_state.active_orders()]
    lines = ["<b>📋 Заявки</b>", ""]
    rows = []
    if not orders:
        lines.append("Нет активных заявок.")
    for o in orders:
        lines.append(order_summary_line(o))
        if not o.ready_waiter:
            rows.append([InlineKeyboardButton(text=f"✅ Готов — Стол {o.table}", callback_data=f"ready_w:{o.id}")])
    lines.append(recommendation_text(chat_state))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_hookah(chat_state: ChatState, chief: bool) -> tuple[str, InlineKeyboardMarkup]:
    orders = [o for o in chat_state.active_orders() if o.hookah]
    title = "👑 Контроль кальянщиков" if chief else "💨 Заявки на кальян"
    lines = [f"<b>{title}</b>", ""]
    rows = []
    if not orders:
        lines.append("Нет заявок с кальяном.")
    for o in orders:
        if not o.hookah_name:
            lines.append(f"Стол {o.table} — не взято")
            rows.append([InlineKeyboardButton(text=f"🫴 Взять — Стол {o.table}", callback_data=f"take_hookah:{o.id}")])
        elif not o.ready_hookah:
            lines.append(f"Стол {o.table} — {o.hookah_item} — готовит {o.hookah_name}")
            rows.append([InlineKeyboardButton(text=f"✅ Готов — Стол {o.table}", callback_data=f"ready_h:{o.id}")])
        else:
            lines.append(f"Стол {o.table} — {o.hookah_item} — ✅ готов ({o.hookah_name})")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_music(chat_state: ChatState) -> tuple[str, InlineKeyboardMarkup]:
    orders = [o for o in chat_state.active_orders() if o.track]
    lines = ["<b>🎵 Треки</b>", ""]
    rows = []
    if not orders:
        lines.append("Нет заявок с треком.")
    for o in orders:
        status = "✅ готов" if o.ready_track else "⏳ ищем"
        song = f" — «{o.congrats_text}»" if o.congrats_text else ""
        lines.append(f"Стол {o.table}{song} — {status}")
        if not o.ready_track:
            rows.append([InlineKeyboardButton(text=f"✅ Готов — Стол {o.table}", callback_data=f"ready_t:{o.id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_dancer(chat_state: ChatState) -> tuple[str, InlineKeyboardMarkup]:
    orders = chat_state.active_orders()
    lines = ["<b>💃 Мои выносы</b>", ""]
    rows = []
    if not orders:
        lines.append("Пока пусто.")
    for o in orders:
        lines.append(order_summary_line(o))
        if o.dancer and not o.ready_dancer:
            rows.append([InlineKeyboardButton(text=f"✅ Готовы — Стол {o.table}", callback_data=f"ready_dc:{o.id}")])
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
        if o.announce_text:
            lines.append(f"    💬 «{o.announce_text}»")
        text = "⏸ На паузе" if chat_state.paused else f"📣 Объявить — Стол {o.table}"
        rows.append([InlineKeyboardButton(text=text, callback_data=f"announce:{o.id}")])
    lines.append("")
    lines.append("<b>Ожидают готовности:</b>")
    if not waiting:
        lines.append("Пусто.")
    for o in waiting:
        lines.append(order_summary_line(o))
        if o.no_waiter and not o.ready_mc:
            rows.append([InlineKeyboardButton(text=f"✅ Готов — Стол {o.table}", callback_data=f"ready_mc:{o.id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_manager(chat_state: ChatState) -> tuple[str, InlineKeyboardMarkup]:
    orders = chat_state.active_orders()
    lines = ["<b>🧑‍💼 Менеджер / всё сразу</b>", ""]
    rows = []
    if chat_state.current_show:
        lines.append(f"🎬 Идёт номер: <b>{chat_state.current_show}</b> (через «Программа»)")
    else:
        pause_text = "▶ Возобновить" if chat_state.paused else "⏸ Пауза"
        rows.append([InlineKeyboardButton(text=pause_text, callback_data="toggle_pause")])
        lines.append("⏸ Пауза активна" if chat_state.paused else "Очередь активна")
    lines.append("")
    lines.append("<b>Очередь (все заявки, все статусы):</b>")
    if not orders:
        lines.append("Пусто.")
    for o in orders:
        lines.append(order_summary_line(o))
        if o.status == "announced":
            rows.append([InlineKeyboardButton(text=f"🏁 Выполнено — Стол {o.table}", callback_data=f"done:{o.id}")])
    lines.append(recommendation_text(chat_state))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


VIEW_RENDERERS = {
    "waiter": lambda cs, uid: render_waiter(cs, uid),
    "hookah": lambda cs, uid: render_hookah(cs, False),
    "hookah_chief": lambda cs, uid: render_hookah(cs, True),
    "music": lambda cs, uid: render_music(cs),
    "dancer": lambda cs, uid: render_dancer(cs),
    "mc": lambda cs, uid: render_mc(cs),
    "manager": lambda cs, uid: render_manager(cs),
}


async def show_view(bot: Bot, chat_id: int, view: str, user_id: int) -> None:
    chat_state = get_state(chat_id)
    text, kb = VIEW_RENDERERS[view](chat_state, user_id)
    msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    chat_state.view_message_ids[f"{view}:{user_id}"] = msg.message_id
    try:
        await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
    except TelegramBadRequest:
        pass  # нет прав закреплять — не критично, просто не закрепится


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


@router.message(F.text == "📋 Просмотреть заявки")
async def btn_waiter(message: Message) -> None:
    await show_view(message.bot, message.chat.id, "waiter", message.from_user.id)


@router.message(F.text == "💨 Заявки на кальян")
async def btn_hookah(message: Message) -> None:
    await show_view(message.bot, message.chat.id, "hookah", message.from_user.id)


@router.message(F.text == "👑 Контроль кальянщиков")
async def btn_hookah_chief(message: Message) -> None:
    await show_view(message.bot, message.chat.id, "hookah_chief", message.from_user.id)


@router.message(F.text == "🎵 Треки")
async def btn_music(message: Message) -> None:
    await show_view(message.bot, message.chat.id, "music", message.from_user.id)


@router.message(F.text == "💃 Мои выносы")
async def btn_dancer(message: Message) -> None:
    await show_view(message.bot, message.chat.id, "dancer", message.from_user.id)


@router.message(F.text == "🎤 Экран MC")
async def btn_mc(message: Message) -> None:
    await show_view(message.bot, message.chat.id, "mc", message.from_user.id)


@router.message(F.text == "🧑‍💼 Менеджер")
async def btn_manager(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await show_view(message.bot, message.chat.id, "manager", message.from_user.id)


@router.message(F.text == "👀 Всё сразу")
async def btn_overview(message: Message) -> None:
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


@router.callback_query(F.data.startswith("take_hookah:"))
async def on_take_hookah(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Не найдено", show_alert=True)
        return
    if order.hookah_name:
        await callback.answer("Уже взято", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=item, callback_data=f"hookahclaim:{order_id}:{item}")] for item in HOOKAH_MENU]
    await callback.bot.send_message(
        callback.message.chat.id,
        f"Стол {order.table} — выбери вкус:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hookahclaim:"))
async def on_hookah_claim(callback: CallbackQuery) -> None:
    _, order_id_str, item = callback.data.split(":", 2)
    order_id = int(order_id_str)
    chat_id = callback.message.chat.id
    chat_state = get_state(chat_id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Не найдено", show_alert=True)
        return
    if order.hookah_name:
        await callback.message.delete()
        await callback.answer("Уже взято кем-то другим", show_alert=True)
        return
    order.hookah_item = item
    order.hookah_name = callback.from_user.full_name
    await callback.message.delete()
    await refresh_all_views(callback.bot, chat_id)
    await callback.answer(f"Взял: {item}")


@router.callback_query(F.data.startswith("ready_h:"))
async def on_ready_hookah(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Не найдено", show_alert=True)
        return
    order.ready_hookah = True
    if order.is_fully_ready():
        order.status = "ready"
        order.ready_at = datetime.now(TIMEZONE)
    await refresh_all_views(callback.bot, callback.message.chat.id)
    await callback.answer("Готов")


@router.callback_query(F.data.startswith("ready_t:"))
async def on_ready_track(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Не найдено", show_alert=True)
        return
    order.ready_track = True
    order.music_name = callback.from_user.full_name
    if order.is_fully_ready():
        order.status = "ready"
        order.ready_at = datetime.now(TIMEZONE)
    await refresh_all_views(callback.bot, callback.message.chat.id)
    await callback.answer("Трек готов")


@router.callback_query(F.data.startswith("ready_mc:"))
async def on_ready_mc(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Не найдено", show_alert=True)
        return
    order.ready_mc = True
    if order.is_fully_ready():
        order.status = "ready"
        order.ready_at = datetime.now(TIMEZONE)
    await refresh_all_views(callback.bot, callback.message.chat.id)
    await callback.answer("Готов")


@router.callback_query(F.data.startswith("ready_dc:"))
async def on_ready_dancer(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Не найдено", show_alert=True)
        return
    order.ready_dancer = True
    if order.is_fully_ready():
        order.status = "ready"
        order.ready_at = datetime.now(TIMEZONE)
    await refresh_all_views(callback.bot, callback.message.chat.id)
    await callback.answer("Готовы")


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
# ПРОГРАММА ВЕЧЕРА (расписание номеров, админ)
# ---------------------------------------------------------------------------

# Шаблон по умолчанию — то, что предложится при первой настройке.
# Дальше последняя подтверждённая программа сама становится шаблоном на завтра.
DEFAULT_PROGRAM_TEMPLATE = [
    {"time": "23:30", "name": "Интро"},
    {"time": "00:00", "name": "Выступление артиста"},
    {"time": "00:30", "name": "Интро 2"},
    {"time": "01:00", "name": "Вау-эффект"},
]
program_template: list[dict] = [dict(x) for x in DEFAULT_PROGRAM_TEMPLATE]


class ProgramSetup(StatesGroup):
    entering_lines = State()


def parse_program_text(text: str) -> list[dict]:
    """Строки вида 'ЧЧ:ММ Название' по одной на строку."""
    items = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and ":" in parts[0]:
            items.append({"time": parts[0], "name": parts[1]})
        else:
            items.append({"time": "", "name": line})
    return items


def program_setup_prompt_text() -> str:
    example = "\n".join(f"{s['time']} {s['name']}" for s in DEFAULT_PROGRAM_TEMPLATE)
    return (
        "Пришли программу на сегодня, по одной строке на номер, формат «время название»:\n\n"
        f"<code>{example}</code>"
    )


@router.message(F.text == "🎬 Программа")
async def btn_program_menu(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    chat_state = get_state(message.chat.id)
    if not chat_state.show_program:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Использовать вчерашнюю программу", callback_data="prog:use_template")],
            [InlineKeyboardButton(text="✏️ Ввести заново", callback_data="prog:new")],
        ])
        await message.answer("Программа на сегодня ещё не настроена.", reply_markup=kb)
        return
    await show_program_screen(message.bot, message.chat.id)


@router.callback_query(F.data == "prog:use_template")
async def on_prog_use_template(callback: CallbackQuery) -> None:
    chat_state = get_state(callback.message.chat.id)
    chat_state.show_program = [dict(x, started=False, done=False) for x in program_template]
    chat_state.current_show_index = None
    await callback.message.delete()
    await show_program_screen(callback.bot, callback.message.chat.id)
    await callback.answer()


@router.callback_query(F.data == "prog:new")
async def on_prog_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProgramSetup.entering_lines)
    await callback.message.edit_text(program_setup_prompt_text(), parse_mode=ParseMode.HTML)
    await callback.answer()


@router.message(ProgramSetup.entering_lines)
async def enter_program_lines(message: Message, state: FSMContext) -> None:
    global program_template
    items = parse_program_text(message.text)
    if not items:
        await message.reply("Не понял формат, попробуй ещё раз.")
        return
    chat_state = get_state(message.chat.id)
    chat_state.show_program = [dict(x, started=False, done=False) for x in items]
    chat_state.current_show_index = None
    program_template = [dict(x) for x in items]  # запоминаем как шаблон на будущее
    await state.clear()
    await try_delete(message)
    await show_program_screen(message.bot, message.chat.id)


def render_program(chat_state: ChatState) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["<b>🎬 Программа вечера</b>", ""]
    rows = []
    active_idx = chat_state.current_show_index

    for i, seg in enumerate(chat_state.show_program):
        mark = "▶️ " if i == active_idx else ("✅ " if seg["done"] else "▫️ ")
        time_part = f"{seg['time']} — " if seg["time"] else ""
        lines.append(f"{mark}{time_part}{seg['name']}")

    lines.append("")
    if active_idx is not None:
        lines.append("Очередь выносов на паузе, пока номер идёт.")
        rows.append([InlineKeyboardButton(text="⏹ Номер закончился", callback_data="prog_end")])
    else:
        next_idx = next((i for i, s in enumerate(chat_state.show_program) if not s["done"]), None)
        if next_idx is not None:
            rows.append([InlineKeyboardButton(
                text=f"▶ Начать: {chat_state.show_program[next_idx]['name']}",
                callback_data=f"prog_start:{next_idx}",
            )])
        else:
            lines.append("Программа на сегодня завершена.")

    rows.append([
        InlineKeyboardButton(text="⏩ +5 мин", callback_data="prog_shift:5"),
        InlineKeyboardButton(text="+10", callback_data="prog_shift:10"),
        InlineKeyboardButton(text="+15", callback_data="prog_shift:15"),
        InlineKeyboardButton(text="+30", callback_data="prog_shift:30"),
    ])
    rows.append([InlineKeyboardButton(text="✏️ Настроить заново", callback_data="prog:new")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def show_program_screen(bot: Bot, chat_id: int) -> None:
    chat_state = get_state(chat_id)
    text, kb = render_program(chat_state)
    if chat_state.program_message_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=chat_state.program_message_id, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except TelegramBadRequest:
            pass
    msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    chat_state.program_message_id = msg.message_id


@router.callback_query(F.data.startswith("prog_start:"))
async def on_prog_start(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Только админ", show_alert=True)
        return
    idx = int(callback.data.split(":")[1])
    chat_id = callback.message.chat.id
    chat_state = get_state(chat_id)
    chat_state.current_show_index = idx
    chat_state.show_program[idx]["started"] = True
    chat_state.current_show = chat_state.show_program[idx]["name"]
    chat_state.paused = True

    tags = " ".join([f"@{MC_USERNAME}", f"@{DJ_USERNAME}"] + [f"@{u}" for u in DANCER_USERNAMES])
    await callback.bot.send_message(
        chat_id,
        f"🎬 <b>Идёт номер: {chat_state.current_show}</b>\nОчередь выносов на паузе.\n\n{tags}",
        parse_mode=ParseMode.HTML,
    )
    await refresh_all_views(callback.bot, chat_id)
    await show_program_screen(callback.bot, chat_id)
    await callback.answer("Начали")


@router.callback_query(F.data == "prog_end")
async def on_prog_end(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Только админ", show_alert=True)
        return
    chat_id = callback.message.chat.id
    chat_state = get_state(chat_id)
    idx = chat_state.current_show_index
    if idx is not None:
        chat_state.show_program[idx]["done"] = True
    chat_state.current_show_index = None
    chat_state.current_show = None
    chat_state.paused = False
    await refresh_all_views(callback.bot, chat_id)
    await show_program_screen(callback.bot, chat_id)
    await callback.answer("Возобновлено")


@router.callback_query(F.data.startswith("prog_shift:"))
async def on_prog_shift(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Только админ", show_alert=True)
        return
    minutes = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    shifted = 0
    for seg in chat_state.show_program:
        if seg["done"] or seg["started"] or not seg["time"]:
            continue
        try:
            h, m = map(int, seg["time"].split(":"))
            total = (h * 60 + m + minutes) % (24 * 60)
            seg["time"] = f"{total // 60:02d}:{total % 60:02d}"
            shifted += 1
        except ValueError:
            continue
    await show_program_screen(callback.bot, callback.message.chat.id)
    await callback.answer(f"Сдвинул на +{minutes} мин ({shifted} номеров)")


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


# ---------------------------------------------------------------------------
# РОЛИ СОТРУДНИКОВ (вход по коду; админ может посмотреть коды и назначить вручную)
# ---------------------------------------------------------------------------


@router.message(Command("codes"))
async def cmd_codes(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    lines = ["<b>Коды ролей:</b>"]
    for code, role in ROLE_CODES.items():
        lines.append(f"{code} → {ROLE_LABELS.get(role, role)}")
    lines.append("")
    lines.append("Поменять коды: переменная ROLE_CODES на Railway, формат waiter=1111,hookah=2222,...")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("setrole"))
async def cmd_setrole(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3 or parts[2] not in ROLE_LABELS:
        await message.reply("Формат: /setrole USER_ID роль (waiter/hookah/hookah_chief/mc/dancer/music/admin)")
        return
    target_id = int(parts[1])
    user_roles[target_id] = parts[2]
    await message.reply(f"Готово: {target_id} → {ROLE_LABELS[parts[2]]}")


# Подстраховка: если написали "старт"/"start"/"/ start" с опечаткой или
# пробелом — всё равно откроем меню, а не будем молчать
@router.message(F.text.lower().in_({"старт", "start", "/ start", "меню", "menu"}))
async def fallback_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await cmd_start(message, state)


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

    # Регистрируем команду в меню Telegram (кнопка "/" рядом с полем ввода),
    # чтобы /start можно было вызвать в один тап, даже если чат не новый
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть меню / выбрать роль"),
    ])
    # Значок меню рядом с полем ввода — всегда виден, по одному тапу
    # открывает список команд (и /start), печатать ничего не нужно
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    asyncio.create_task(daily_stats_job(bot))

    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
