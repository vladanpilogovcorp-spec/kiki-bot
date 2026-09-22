"""
Бот очереди выносов для клуба «Кики».

Логика:
- Любой участник группы может подать заявку через /new (мастер из нескольких шагов).
- Заявка появляется в группе одной карточкой со статусом «Собираются».
- Кнопка «Готовы» под карточкой — бригада жмёт сама, когда реально готова.
  После этого статус меняется на «Готовы», и время готовности используется
  для очерёдности (а не время подачи заявки).
- VIP-заявки всегда идут выше остальных, независимо от времени.
- Кнопка «Объявить» доступна только когда заявка «Готовы» и очередь не на паузе.
- Менеджер командой /pause ставит очередь на паузу (например, начался номер
  шоу-программы), /resume — снимает. Пока пауза активна, кнопка «Объявить»
  заблокирована для всех заявок.
- Если в заявке указан заказ песни — карточка автоматически тегает MC и
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
from aiogram.filters import Command, CommandObject
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

# Юзернеймы (без @), которых тегать при заказе песни
MC_USERNAME = os.environ.get("MC_USERNAME", "MC_username")
MUSIC_USERNAME = os.environ.get("MUSIC_USERNAME", "music_username")

# Кто может ставить паузу (менеджеры). Укажи Telegram user_id через запятую
# в переменной окружения MANAGER_IDS, например: "123456789,987654321"
# Если оставить пустым — паузу сможет ставить кто угодно (проще для теста).
MANAGER_IDS = {
    int(x) for x in os.environ.get("MANAGER_IDS", "").split(",") if x.strip()
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kiki-bot")

# ---------------------------------------------------------------------------
# СОСТОЯНИЕ (в памяти, на группу/чат)
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
    author_name: str = ""


class ChatState:
    def __init__(self) -> None:
        self.orders: dict[int, Order] = {}
        self.next_id = 1
        self.paused = False

    def add_order(self, table: str, kind: str, vip: bool, song: Optional[str], author_name: str) -> Order:
        order = Order(
            id=self.next_id,
            table=table,
            kind=kind,
            vip=vip,
            song=song,
            author_name=author_name,
        )
        self.orders[order.id] = order
        self.next_id += 1
        return order

    def sorted_active(self) -> list[Order]:
        active = [o for o in self.orders.values() if o.status not in ("done",)]

        def sort_key(o: Order):
            ref_time = o.ready_at or o.created_at
            return (0 if o.vip else 1, ref_time)

        return sorted(active, key=sort_key)


# chat_id -> ChatState
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
    await message.reply("Кто подаёт заявку?", reply_markup=kind_keyboard())


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
    await message.reply("Это VIP-стол?", reply_markup=yes_no_keyboard("vip"))


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
    await finalize_order(callback.message, state, callback.from_user.full_name)
    await callback.answer()


@router.message(NewOrder.entering_song_name)
async def enter_song_name(message: Message, state: FSMContext) -> None:
    await state.update_data(song=message.text.strip())
    await finalize_order(message, state, message.from_user.full_name)


async def finalize_order(message: Message, state: FSMContext, author_name: str) -> None:
    data = await state.get_data()
    chat_state = get_state(message.chat.id)
    order = chat_state.add_order(
        table=data["table"],
        kind=data["kind"],
        vip=data.get("vip", False),
        song=data.get("song"),
        author_name=author_name,
    )
    await state.clear()
    await post_order_card(message.bot, message.chat.id, order)


# ---------------------------------------------------------------------------
# Карточка заявки в группе + кнопки действий
# ---------------------------------------------------------------------------


def render_card_text(order: Order, paused: bool) -> str:
    kind_label = "Вынос" if order.kind == "waiter" else "Кальян"
    lines = [f"<b>Стол {order.table}</b> — {kind_label}"]
    if order.vip:
        lines.append("⭐ <b>VIP</b>")
    status_map = {
        "collecting": "🟡 Собираются",
        "ready": "🟢 Готовы, ждут объявления",
        "announced": "🟣 Объявлено",
        "done": "✅ Выполнено",
    }
    lines.append(status_map[order.status])
    if order.song:
        lines.append(f"🎵 Заказ песни: {order.song}")
        lines.append(f"cc: @{MC_USERNAME} @{MUSIC_USERNAME}")
    if paused and order.status == "ready":
        lines.append("⏸ <i>Очередь на паузе — идёт номер</i>")
    return "\n".join(lines)


def render_card_keyboard(order: Order, paused: bool) -> InlineKeyboardMarkup:
    buttons = []
    if order.status == "collecting":
        buttons.append(InlineKeyboardButton(text="✅ Готовы", callback_data=f"ready:{order.id}"))
    elif order.status == "ready":
        announce_btn = InlineKeyboardButton(
            text="📣 Объявить" if not paused else "⏸ На паузе",
            callback_data=f"announce:{order.id}",
        )
        buttons.append(announce_btn)
    elif order.status == "announced":
        buttons.append(InlineKeyboardButton(text="🏁 Выполнено", callback_data=f"done:{order.id}"))

    if order.status != "done":
        buttons.append(InlineKeyboardButton(text="✕ Отменить", callback_data=f"cancel:{order.id}"))

    return InlineKeyboardMarkup(inline_keyboard=[buttons] if buttons else [])


async def post_order_card(bot: Bot, chat_id: int, order: Order) -> None:
    chat_state = get_state(chat_id)
    text = render_card_text(order, chat_state.paused)
    kb = render_card_keyboard(order, chat_state.paused)
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)


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
    await callback.message.edit_text(
        render_card_text(order, chat_state.paused),
        reply_markup=render_card_keyboard(order, chat_state.paused),
        parse_mode=ParseMode.HTML,
    )
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
    await callback.message.edit_text(
        render_card_text(order, chat_state.paused),
        reply_markup=render_card_keyboard(order, chat_state.paused),
        parse_mode=ParseMode.HTML,
    )
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
    await callback.message.edit_text(
        render_card_text(order, chat_state.paused),
        reply_markup=render_card_keyboard(order, chat_state.paused),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("cancel:"))
async def on_cancel(callback: CallbackQuery) -> None:
    order_id = int(callback.data.split(":")[1])
    chat_state = get_state(callback.message.chat.id)
    if order_id in chat_state.orders:
        del chat_state.orders[order_id]
    await callback.message.edit_text("❌ Заявка отменена")
    await callback.answer("Отменено")


# ---------------------------------------------------------------------------
# Менеджерские команды: пауза очереди на время шоу-номера
# ---------------------------------------------------------------------------


def is_manager(user_id: int) -> bool:
    if not MANAGER_IDS:
        return True  # ограничение выключено — для простоты теста
    return user_id in MANAGER_IDS


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    if not is_manager(message.from_user.id):
        await message.reply("Пауза доступна только менеджеру.")
        return
    chat_state = get_state(message.chat.id)
    chat_state.paused = True
    await message.reply("⏸ Очередь на паузе. Объявления заблокированы до /resume.")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    if not is_manager(message.from_user.id):
        await message.reply("Возобновление доступно только менеджеру.")
        return
    chat_state = get_state(message.chat.id)
    chat_state.paused = False
    await message.reply("▶ Очередь возобновлена.")


@router.message(Command("queue"))
async def cmd_queue(message: Message) -> None:
    chat_state = get_state(message.chat.id)
    active = chat_state.sorted_active()
    if not active:
        await message.reply("Очередь пуста.")
        return
    lines = ["<b>Текущая очередь:</b>"]
    for i, order in enumerate(active, start=1):
        mark = "⭐" if order.vip else str(i)
        kind_label = "Вынос" if order.kind == "waiter" else "Кальян"
        lines.append(f"{mark}. Стол {order.table} — {kind_label} — {order.status}")
    await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.reply(
        "Команды:\n"
        "/new — подать заявку на вынос/кальян\n"
        "/queue — посмотреть текущую очередь\n"
        "/pause — менеджер ставит очередь на паузу (идёт номер)\n"
        "/resume — менеджер возобновляет очередь"
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
