"""
Бот очереди выносов для клуба «Кики».

Главное отличие от первой версии: бот ведёт ОДНО сообщение с очередью на
чат, которое сам редактирует при любом изменении, вместо того чтобы
присылать отдельную карточку на каждую заявку. Это сообщение бот старается
закрепить (если у него есть права администратора в группе) — так очередь
всегда под рукой, а не теряется среди сотен сообщений.

Логика:
- Любой участник группы подаёт заявку через /new (мастер из нескольких шагов).
- Заявка добавляется в общий список со статусом «Собираются», и сразу же
  обновляется единое сообщение-очередь.
- Кнопка «Готовы» под нужной заявкой в этом сообщении — бригада жмёт сама,
  когда реально готова. Время готовности используется для очерёдности
  (а не время подачи заявки).
- VIP-заявки всегда идут выше остальных, независимо от времени.
- Кнопка «Объявить» доступна только когда заявка «Готовы» и очередь не на
  паузе.
- Менеджер командой /pause ставит очередь на паузу (например, начался номер
  шоу-программы), /resume — снимает.
- Если в заявке указан заказ песни — в очереди она помечается тегами MC и
  ответственного за музыку (юзернеймы задаются в конфиге ниже).

Хранение — в памяти процесса (для теста). Для продакшена стоит заменить
словари на настоящую БД (SQLite/Postgres), см. README.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

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
    Message,
)

# ---------------------------------------------------------------------------
# КОНФИГ — поправь под себя
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")

MC_USERNAME = os.environ.get("MC_USERNAME", "MC_username")
MUSIC_USERNAME = os.environ.get("MUSIC_USERNAME", "music_username")

MANAGER_IDS = {
    int(x) for x in os.environ.get("MANAGER_IDS", "").split(",") if x.strip()
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kiki-bot")

# ---------------------------------------------------------------------------
# СОСТОЯНИЕ (в памяти, на чат)
# ---------------------------------------------------------------------------


@dataclass
class Order:
    id: int
    table: str
    kind: str  # "waiter" | "hookah"
    vip: bool
    song: Optional[str]
    status: str = "collecting"  # collecting -> ready -> announced -> done
    created_at: datetime = field(default_factory=datetime.now)
    ready_at: Optional[datetime] = None


class ChatState:
    def __init__(self) -> None:
        self.orders: dict[int, Order] = {}
        self.next_id = 1
        self.paused = False
        self.queue_message_id: Optional[int] = None

    def add_order(self, table: str, kind: str, vip: bool, song: Optional[str]) -> Order:
        order = Order(id=self.next_id, table=table, kind=kind, vip=vip, song=song)
        self.orders[order.id] = order
        self.next_id += 1
        return order

    def visible_orders(self) -> list[Order]:
        active = [o for o in self.orders.values() if o.status != "done"]

        def sort_key(o: Order):
            ref_time = o.ready_at or o.created_at
            stage_rank = {"collecting": 1, "ready": 0, "announced": 2}[o.status]
            return (stage_rank, 0 if o.vip else 1, ref_time)

        return sorted(active, key=sort_key)


chat_states: dict[int, ChatState] = {}


def get_state(chat_id: int) -> ChatState:
    if chat_id not in chat_states:
        chat_states[chat_id] = ChatState()
    return chat_states[chat_id]


# ---------------------------------------------------------------------------
# FSM для мастера подачи заявки (/new)
# ---------------------------------------------------------------------------


class NewOrder(StatesGroup):
    choosing_kind = State()
    entering_table = State()
    asking_vip = State()
    asking_song = State()
    entering_song_name = State()


router = Router()


def kind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🍾 Официант / вынос", callback_data="kind:waiter"),
                InlineKeyboardButton(text="💨 Кальянщик", callback_data="kind:hookah"),
            ]
        ]
    )


def yes_no_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data=f"{prefix}:yes"),
                InlineKeyboardButton(text="Нет", callback_data=f"{prefix}:no"),
            ]
        ]
    )


@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext) -> None:
    await state.set_state(NewOrder.choosing_kind)
    wizard_msg = await message.reply("Кто подаёт заявку?", reply_markup=kind_keyboard())
    await state.update_data(wizard_message_id=wizard_msg.message_id)


@router.callback_query(NewOrder.choosing_kind, F.data.startswith("kind:"))
async def choose_kind(callback: CallbackQuery, state: FSMContext) -> None:
    kind = callback.data.split(":")[1]
    await state.update_data(kind=kind)
    await state.set_state(NewOrder.entering_table)
    await callback.message.edit_text("Номер стола? Напиши в чат цифрой.")
    await callback.answer()


@router.message(NewOrder.entering_table)
async def enter_table(message: Message, state: FSMContext) -> None:
    table = message.text.strip()
    await state.update_data(table=table)
    await state.set_state(NewOrder.asking_vip)
    wizard_msg = await message.reply("Это VIP-стол?", reply_markup=yes_no_keyboard("vip"))
    await state.update_data(wizard_message_id=wizard_msg.message_id)
    await try_delete(message)


@router.callback_query(NewOrder.asking_vip, F.data.startswith("vip:"))
async def ask_vip(callback: CallbackQuery, state: FSMContext) -> None:
    vip = callback.data.split(":")[1] == "yes"
    await state.update_data(vip=vip)
    await state.set_state(NewOrder.asking_song)
    await callback.message.edit_text("Есть заказ песни?", reply_markup=yes_no_keyboard("song"))
    await callback.answer()


@router.callback_query(NewOrder.asking_song, F.data.startswith("song:"))
async def ask_song(callback: CallbackQuery, state: FSMContext) -> None:
    has_song = callback.data.split(":")[1] == "yes"
    if has_song:
        await state.set_state(NewOrder.entering_song_name)
        await callback.message.edit_text("Название песни / исполнитель — напиши в чат.")
        await callback.answer()
        return

    await state.update_data(song=None)
    await finalize_order(callback.message, state)
    await callback.answer()


@router.message(NewOrder.entering_song_name)
async def enter_song_name(message: Message, state: FSMContext) -> None:
    await state.update_data(song=message.text.strip())
    await finalize_order(message, state)
    await try_delete(message)


async def try_delete(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def finalize_order(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    chat_id = message.chat.id
    chat_state = get_state(chat_id)
    chat_state.add_order(
        table=data["table"],
        kind=data["kind"],
        vip=data.get("vip", False),
        song=data.get("song"),
    )

    wizard_message_id = data.get("wizard_message_id")
    await state.clear()

    bot = message.bot
    if wizard_message_id:
        try:
            await bot.delete_message(chat_id, wizard_message_id)
        except TelegramBadRequest:
            pass

    await refresh_queue_message(bot, chat_id)


# ---------------------------------------------------------------------------
# Единое сообщение очереди — рендер и обновление
# ---------------------------------------------------------------------------


STATUS_EMOJI = {"collecting": "🟡", "ready": "🟢", "announced": "🟣"}
STATUS_LABEL = {
    "collecting": "Собираются",
    "ready": "Готовы, ждут объявления",
    "announced": "Объявлено",
}


def render_queue_text(chat_state: ChatState) -> str:
    orders = chat_state.visible_orders()
    lines = ["<b>📋 Очередь выносов</b>"]
    if chat_state.paused:
        lines.append("⏸ <b>Пауза — сейчас идёт номер</b>")
    lines.append("")

    if not orders:
        lines.append("Очередь пуста.")
    else:
        for i, o in enumerate(orders, start=1):
            kind_label = "Вынос" if o.kind == "waiter" else "Кальян"
            mark = "⭐" if o.vip else str(i)
            lines.append(f"{mark}. Стол {o.table} — {kind_label} {STATUS_EMOJI[o.status]} {STATUS_LABEL[o.status]}")
            if o.song:
                lines.append(f"    🎵 {o.song} — cc: @{MC_USERNAME} @{MUSIC_USERNAME}")

    lines.append("")
    lines.append("<i>/new — новая заявка · /pause /resume — управление менеджера</i>")
    return "\n".join(lines)


def render_queue_keyboard(chat_state: ChatState) -> InlineKeyboardMarkup:
    orders = chat_state.visible_orders()
    rows = []
    for o in orders:
        kind_label = "Вынос" if o.kind == "waiter" else "Кальян"
        label = f"Стол {o.table} ({kind_label})"
        if o.status == "collecting":
            rows.append([InlineKeyboardButton(text=f"✅ Готовы — {label}", callback_data=f"ready:{o.id}")])
        elif o.status == "ready":
            text = "⏸ На паузе" if chat_state.paused else f"📣 Объявить — {label}"
            rows.append([InlineKeyboardButton(text=text, callback_data=f"announce:{o.id}")])
        elif o.status == "announced":
            rows.append([InlineKeyboardButton(text=f"🏁 Выполнено — {label}", callback_data=f"done:{o.id}")])
        rows.append([InlineKeyboardButton(text=f"✕ Отменить — {label}", callback_data=f"cancel:{o.id}")])

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else InlineKeyboardMarkup(inline_keyboard=[])


async def refresh_queue_message(bot: Bot, chat_id: int) -> None:
    chat_state = get_state(chat_id)
    text = render_queue_text(chat_state)
    kb = render_queue_keyboard(chat_state)

    if chat_state.queue_message_id is None:
        msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        chat_state.queue_message_id = msg.message_id
        try:
            await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        except TelegramBadRequest:
            logger.warning("Не удалось закрепить сообщение очереди в чате %s", chat_id)
        return

    try:
        await bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=chat_state.queue_message_id,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        chat_state.queue_message_id = msg.message_id
        try:
            await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        except TelegramBadRequest:
            pass


# ---------------------------------------------------------------------------
# Действия по заявкам
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("ready:"))
async def on_ready(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    order.status = "ready"
    order.ready_at = datetime.now()
    await refresh_queue_message(callback.bot, callback.message.chat.id)
    await callback.answer("Отмечено: готовы")


@router.callback_query(F.data.startswith("announce:"))
async def on_announce(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if chat_state.paused:
        await callback.answer("Очередь на паузе — сейчас идёт номер", show_alert=True)
        return
    order.status = "announced"
    await refresh_queue_message(callback.bot, callback.message.chat.id)
    await callback.answer("Объявлено")


@router.callback_query(F.data.startswith("done:"))
async def on_done(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    order = chat_state.orders.get(order_id)
    if not order:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    order.status = "done"
    await refresh_queue_message(callback.bot, callback.message.chat.id)
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("cancel:"))
async def on_cancel(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    if order_id in chat_state.orders:
        del chat_state.orders[order_id]
    await refresh_queue_message(callback.bot, callback.message.chat.id)
    await callback.answer("Отменено")


# ---------------------------------------------------------------------------
# Менеджерские команды
# ---------------------------------------------------------------------------


def is_manager(user_id: int) -> bool:
    if not MANAGER_IDS:
        return True
    return user_id in MANAGER_IDS


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    if not is_manager(message.from_user.id):
        await message.reply("Пауза доступна только менеджеру.")
        return
    chat_state = get_state(message.chat.id)
    chat_state.paused = True
    await refresh_queue_message(message.bot, message.chat.id)
    await try_delete(message)


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    if not is_manager(message.from_user.id):
        await message.reply("Возобновление доступно только менеджеру.")
        return
    chat_state = get_state(message.chat.id)
    chat_state.paused = False
    await refresh_queue_message(message.bot, message.chat.id)
    await try_delete(message)


@router.message(Command("queue"))
async def cmd_queue(message: Message) -> None:
    await refresh_queue_message(message.bot, message.chat.id)
    await try_delete(message)


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.reply(
        "Команды:\n"
        "/new — подать заявку на вынос/кальян\n"
        "/queue — показать/поднять сообщение с очередью\n"
        "/pause — менеджер ставит очередь на паузу (идёт номер)\n"
        "/resume — менеджер возобновляет очередь\n\n"
        "Очередь всегда живёт в ОДНОМ сообщении, которое бот сам обновляет "
        "и старается закрепить сверху чата — выдай боту права администратора "
        "в группе, чтобы закрепление работало."
    )


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


async def main() -> None:
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise RuntimeError(
            "Укажи токен бота: переменная окружения BOT_TOKEN "
            "или впиши прямо в код (см. README)."
        )

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
