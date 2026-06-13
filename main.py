import asyncio
import logging
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    ChatMemberUpdated, Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramAPIError
from aiogram.enums import ChatMemberStatus
from aiogram.client.session.aiohttp import AiohttpSession
from openai import AsyncOpenAI
from openai import APIError, APITimeoutError

from config import (
    BOT_TOKEN, DEFAULT_ACTION_MODE, VERIFICATION_TIMEOUT,
    AUTO_DELETE_UNVERIFIED,
    LLM_API_KEY, LLM_ACCESS_ID, LLM_MODEL, LLM_TIMEOUT
)
from database import (
    init_db, get_chat_settings, set_chat_settings,
    add_pending_verification, remove_pending_verification,
    get_pending_verification, get_all_pending_verifications,
    add_banned_user, remove_banned_user, is_user_banned, get_banned_list
)
from filters import contains_spam, add_stopword, remove_stopword, STOPWORDS

from aiohttp import TCPConnector

import os
os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
os.environ.pop('http_proxy', None)
os.environ.pop('https_proxy', None)
# ─── Логирование ───────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("spam.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ─── Глобальные переменные ─────────────────────────
bot: Optional[Bot] = None
dp = Dispatcher()
router = Router()
dp.include_router(router)

verification_tasks: dict[tuple[int, int], asyncio.Task] = {}
bot_msg_ids = defaultdict(lambda: deque(maxlen=100))

# ─── LLM клиент (инициализируем в main) ───────────
llm_client: Optional[AsyncOpenAI] = None

# ─── Вспомогательные функции ──────────────────────

async def send_and_track(chat_id: int, text: str, **kwargs) -> Optional[Message]:
    if bot is None:
        logger.error("Бот не инициализирован")
        return None
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        bot_msg_ids[chat_id].append(msg.message_id)
        return msg
    except TelegramAPIError as e:
        logger.error(f"Не удалось отправить сообщение в чат {chat_id}: {e}")
        return None

async def get_effective_action_mode(chat_id: int) -> str:
    settings = await get_chat_settings(chat_id)
    return settings["action_mode"] or DEFAULT_ACTION_MODE

async def get_verify_topic_id(chat_id: int) -> Optional[int]:
    settings = await get_chat_settings(chat_id)
    return settings.get("verify_topic_id")

async def kick_user(chat_id: int, user_id: int):
    if bot is None:
        return
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await asyncio.sleep(0.5)
        await bot.unban_chat_member(chat_id, user_id)
        logger.info(f"Пользователь {user_id} удалён из чата {chat_id}")
    except TelegramAPIError as e:
        logger.error(f"Ошибка при удалении пользователя {user_id}: {e}")

async def delete_bot_message(chat_id: int, message_id: int):
    if bot is None:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError as e:
        logger.warning(f"Не удалось удалить сообщение {message_id} в чате {chat_id}: {e}")

async def restart_verification_tasks():
    pendings = await get_all_pending_verifications()
    now = datetime.now()
    for p in pendings:
        remaining = (p["expires_at"] - now).total_seconds()
        if remaining <= 0:
            await kick_user(p["chat_id"], p["user_id"])
            await delete_bot_message(p["chat_id"], p["message_id"])
            await remove_pending_verification(p["user_id"], p["chat_id"])
        else:
            task = asyncio.create_task(
                verification_timer(p["user_id"], p["chat_id"], p["message_id"],
                                   p["message_thread_id"], remaining)
            )
            verification_tasks[(p["chat_id"], p["user_id"])] = task
            logger.info(f"Перезапущен таймер верификации для {p['user_id']} в чате {p['chat_id']} "
                        f"через {remaining:.0f} сек.")

async def verification_timer(user_id: int, chat_id: int, message_id: int,
                             message_thread_id: Optional[int], timeout: float):
    await asyncio.sleep(timeout)
    pending = await get_pending_verification(user_id, chat_id)
    if pending and pending["message_id"] == message_id:
        logger.info(f"Таймаут верификации для {user_id} в чате {chat_id}")
        await kick_user(chat_id, user_id)
        await delete_bot_message(chat_id, message_id)
        await remove_pending_verification(user_id, chat_id)
        verification_tasks.pop((chat_id, user_id), None)
        await add_banned_user(user_id, chat_id)

async def is_admin(chat_id: int, user_id: int) -> bool:
    if bot is None:
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except TelegramAPIError:
        return False

# ─── LLM проверка ────────────────────────────────

async def ask_llm(text: str) -> bool:
    """Возвращает True, если LLM считает сообщение спамом (ответ 'да'), иначе False."""
    if llm_client is None:
        logger.error("LLM клиент не настроен")
        return True  # при ошибке считаем спамом

    prompt = f"Отвечай только 'да' или 'нет'. Является ли это сообщение спамом?\nСообщение: {text}"
    try:
        response = await asyncio.wait_for(
            llm_client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=10,
                temperature=0.0
            ),
            timeout=LLM_TIMEOUT
        )
        answer = response.choices[0].message.content.strip().lower()
        logger.info(f"LLM ответ: {answer}")
        return answer == "да"
    except (APIError, APITimeoutError, asyncio.TimeoutError, Exception) as e:
        logger.warning(f"Ошибка LLM: тип={type(e).__name__},  msg='{e}'. Fallback to spam=True")
        return True

# ─── Команды ───────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📋 Команды управления:\n"
        "/setmode delete|notify_admin — режим реакции на спам\n"
        "/setverifytopic <topic_id> — установить топик для приветствий (в супергруппах)\n"
        "/ban @username — добавить в чёрный список и забанить\n"
        "/unban @username — удалить из чёрного списка и разбанить\n"
        "/bannedlist — показать список забаненных\n"
        "/stopwords — показать список запрещённых слов\n"
        "/addstopword слово — добавить стоп-слово\n"
        "/removestopword слово — удалить стоп-слово\n"
        "/clearlast [1|3|10|all] — удалить последние сообщения бота\n"
        "/llm [on|off|status] — управление проверкой через LLM\n"
        "/verify — подтвердить, что вы человек (если потеряли кнопку)\n"
        "/help — эта справка"
    )
    await send_and_track(message.chat.id, text, message_thread_id=message.message_thread_id)

@router.message(Command("llm"))
async def cmd_llm(message: Message, command: CommandObject):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_admin(chat_id, user_id):
        return await send_and_track(chat_id, "Только администраторы могут управлять LLM.",
                                    message_thread_id=message.message_thread_id)
    args = command.args.strip().lower() if command.args else ""
    settings = await get_chat_settings(chat_id)

    if args in ("on", "1", "true"):
        if not LLM_API_KEY or not LLM_ACCESS_ID:
            return await send_and_track(chat_id, "LLM не настроен: отсутствует API-ключ или Access ID.",
                                        message_thread_id=message.message_thread_id)
        await set_chat_settings(chat_id, use_llm=True)
        await send_and_track(chat_id, "Проверка через LLM включена.",
                             message_thread_id=message.message_thread_id)
    elif args in ("off", "0", "false"):
        await set_chat_settings(chat_id, use_llm=False)
        await send_and_track(chat_id, "Проверка через LLM выключена.",
                             message_thread_id=message.message_thread_id)
    elif args == "status":
        state = "включена" if settings["use_llm"] else "выключена"
        await send_and_track(chat_id, f"LLM проверка: {state}.",
                             message_thread_id=message.message_thread_id)
    else:
        await send_and_track(chat_id, "Использование: /llm [on|off|status]",
                             message_thread_id=message.message_thread_id)

# Остальные команды (setmode, ban, ...) остаются как раньше, только используют send_and_track
@router.message(Command("setmode"))
async def cmd_setmode(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_admin(chat_id, user_id):
        return await send_and_track(chat_id, "Только администраторы могут изменять настройки.",
                                    message_thread_id=message.message_thread_id)
    if message.chat.type not in ("group", "supergroup"):
        return await send_and_track(chat_id, "Команда доступна только в группах.",
                                    message_thread_id=message.message_thread_id)
    args = message.text.split()
    if len(args) != 2 or args[1] not in ("delete", "notify_admin"):
        return await send_and_track(chat_id, "Использование: /setmode delete или /setmode notify_admin",
                                    message_thread_id=message.message_thread_id)
    await set_chat_settings(chat_id, action_mode=args[1])
    await send_and_track(chat_id, f"Режим изменён на {args[1]}",
                         message_thread_id=message.message_thread_id)

@router.message(Command("setverifytopic"))
async def cmd_setverifytopic(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_admin(chat_id, user_id):
        return await send_and_track(chat_id, "Только администраторы могут изменять настройки.",
                                    message_thread_id=message.message_thread_id)
    if message.chat.type != "supergroup":
        return await send_and_track(chat_id, "Топики доступны только в супергруппах.",
                                    message_thread_id=message.message_thread_id)
    args = message.text.split()
    if len(args) != 2:
        return await send_and_track(chat_id, "Использование: /setverifytopic <topic_id>",
                                    message_thread_id=message.message_thread_id)
    try:
        topic_id = int(args[1])
    except ValueError:
        return await send_and_track(chat_id, "Некорректный ID топика.",
                                    message_thread_id=message.message_thread_id)
    await set_chat_settings(chat_id, verify_topic_id=topic_id)
    await send_and_track(chat_id, f"Топик для приветствий установлен: {topic_id}",
                         message_thread_id=message.message_thread_id)

@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_admin(chat_id, user_id):
        return await send_and_track(chat_id, "Только администраторы могут банить.",
                                    message_thread_id=message.message_thread_id)
    if not command.args:
        return await send_and_track(chat_id, "Укажите пользователя: /ban @username или /ban user_id",
                                    message_thread_id=message.message_thread_id)
    target = command.args.strip()
    try:
        if target.startswith("@"):
            member = await bot.get_chat_member(chat_id, target)
            target_id = member.user.id
        else:
            target_id = int(target)
    except Exception:
        return await send_and_track(chat_id, "Не удалось найти пользователя.",
                                    message_thread_id=message.message_thread_id)
    try:
        await bot.ban_chat_member(chat_id, target_id)
    except TelegramAPIError as e:
        return await send_and_track(chat_id, f"Ошибка бана: {e}",
                                    message_thread_id=message.message_thread_id)
    await add_banned_user(target_id, chat_id)
    await send_and_track(chat_id, f"Пользователь {target} забанен и добавлен в чёрный список.",
                         message_thread_id=message.message_thread_id)

@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_admin(chat_id, user_id):
        return await send_and_track(chat_id, "Только администраторы могут разбанивать.",
                                    message_thread_id=message.message_thread_id)
    if not command.args:
        return await send_and_track(chat_id, "Укажите пользователя: /unban @username или /unban user_id",
                                    message_thread_id=message.message_thread_id)
    target = command.args.strip()
    try:
        if target.startswith("@"):
            try:
                member = await bot.get_chat_member(chat_id, target)
                target_id = member.user.id
            except:
                return await send_and_track(chat_id, "Пользователь не найден в чате, укажите числовой ID.",
                                            message_thread_id=message.message_thread_id)
        else:
            target_id = int(target)
    except:
        return await send_and_track(chat_id, "Неверный формат.",
                                    message_thread_id=message.message_thread_id)
    await remove_banned_user(target_id, chat_id)
    try:
        await bot.unban_chat_member(chat_id, target_id)
    except TelegramAPIError as e:
        logger.warning(f"Ошибка разбана {target_id}: {e}")
    await send_and_track(chat_id, f"Пользователь {target} разбанен и удалён из чёрного списка.",
                         message_thread_id=message.message_thread_id)

@router.message(Command("bannedlist"))
async def cmd_bannedlist(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_admin(chat_id, user_id):
        return await send_and_track(chat_id, "Только администраторы могут просматривать чёрный список.",
                                    message_thread_id=message.message_thread_id)
    banned = await get_banned_list(chat_id)
    if not banned:
        return await send_and_track(chat_id, "Чёрный список пуст.",
                                    message_thread_id=message.message_thread_id)
    lines = []
    for uid, date in banned:
        try:
            user = await bot.get_chat(uid)
            name = user.mention_html() if hasattr(user, 'mention_html') else f"ID{uid}"
        except:
            name = f"ID{uid}"
        lines.append(f"{name} — {date}")
    await send_and_track(chat_id, "Забаненные пользователи:\n" + "\n".join(lines),
                         parse_mode="HTML", message_thread_id=message.message_thread_id)

@router.message(Command("stopwords"))
async def cmd_stopwords(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_admin(chat_id, user_id):
        return await send_and_track(chat_id, "Только администраторы могут просматривать стоп-слова.",
                                    message_thread_id=message.message_thread_id)
    if not STOPWORDS:
        return await send_and_track(chat_id, "Список стоп-слов пуст.",
                                    message_thread_id=message.message_thread_id)
    await send_and_track(chat_id, "Стоп-слова:\n" + "\n".join(STOPWORDS),
                         message_thread_id=message.message_thread_id)

@router.message(Command("addstopword"))
async def cmd_addstopword(message: Message, command: CommandObject):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_admin(chat_id, user_id):
        return await send_and_track(chat_id, "Только администраторы могут изменять стоп-слова.",
                                    message_thread_id=message.message_thread_id)
    if not command.args:
        return await send_and_track(chat_id, "Использование: /addstopword слово",
                                    message_thread_id=message.message_thread_id)
    word = command.args.strip()
    add_stopword(word)
    await send_and_track(chat_id, f"Слово '{word}' добавлено в список стоп-слов.",
                         message_thread_id=message.message_thread_id)

@router.message(Command("removestopword"))
async def cmd_removestopword(message: Message, command: CommandObject):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_admin(chat_id, user_id):
        return await send_and_track(chat_id, "Только администраторы могут изменять стоп-слова.",
                                    message_thread_id=message.message_thread_id)
    if not command.args:
        return await send_and_track(chat_id, "Использование: /removestopword слово",
                                    message_thread_id=message.message_thread_id)
    word = command.args.strip()
    remove_stopword(word)
    await send_and_track(chat_id, f"Слово '{word}' удалено из списка стоп-слов.",
                         message_thread_id=message.message_thread_id)

@router.message(Command("clearlast"))
async def cmd_clearlast(message: Message, command: CommandObject):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_admin(chat_id, user_id):
        return await send_and_track(chat_id, "Только администраторы могут удалять сообщения бота.",
                                    message_thread_id=message.message_thread_id)
    count_str = command.args or "1"
    if count_str == "all":
        count = len(bot_msg_ids[chat_id])
    else:
        try:
            count = int(count_str)
        except ValueError:
            return await send_and_track(chat_id, "Использование: /clearlast [1|3|10|all]",
                                        message_thread_id=message.message_thread_id)
    if count <= 0:
        return
    ids_to_delete = []
    for _ in range(min(count, len(bot_msg_ids[chat_id]))):
        ids_to_delete.append(bot_msg_ids[chat_id].pop())
    for msg_id in ids_to_delete:
        await delete_bot_message(chat_id, msg_id)
    await send_and_track(chat_id, f"Удалено {len(ids_to_delete)} сообщений.",
                         message_thread_id=message.message_thread_id)

@router.message(Command("verify"))
async def cmd_verify(message: Message):
    user = message.from_user
    if user.is_bot:
        return await send_and_track(message.chat.id, "Боты не могут проходить верификацию.")
    chat_id = message.chat.id
    user_id = user.id
    pending = await get_pending_verification(user_id, chat_id)
    if not pending:
        return await send_and_track(chat_id, "Вы не находитесь в процессе верификации.")
    task = verification_tasks.pop((chat_id, user_id), None)
    if task:
        task.cancel()
    await remove_pending_verification(user_id, chat_id)
    await delete_bot_message(chat_id, pending["message_id"])
    await send_and_track(chat_id, "Спасибо, вы верифицированы!")
    logger.info(f"Пользователь {user_id} верифицирован через /verify в чате {chat_id}")

# ─── Обработчики событий ──────────────────────────

@router.chat_member()
async def on_chat_member_update(update: ChatMemberUpdated):
    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status
    if new_status == "member" and old_status not in ("member", "creator", "administrator"):
        user = update.new_chat_member.user
        chat_id = update.chat.id
        user_id = user.id

        if await is_user_banned(user_id, chat_id):
            try:
                await bot.ban_chat_member(chat_id, user_id)
                await asyncio.sleep(0.5)
                await bot.unban_chat_member(chat_id, user_id)
            except TelegramAPIError:
                pass
            logger.info(f"Забаненный пользователь {user_id} попытался войти в чат {chat_id} и был удалён.")
            return

        verify_topic_id = await get_verify_topic_id(chat_id)
        user_mention = user.mention_html()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Я человек", callback_data=f"verify_{user_id}")]
        ])
        msg = await send_and_track(
            chat_id,
            f"Добро пожаловать, {user_mention}!\n"
            f"Подтвердите, что вы не бот, нажав кнопку ниже или командой /verify.\n"
            f"У вас {VERIFICATION_TIMEOUT} секунд, иначе вы будете удалены.",
            parse_mode="HTML",
            reply_markup=keyboard,
            message_thread_id=verify_topic_id
        )
        if not msg:
            return
        expires_at = datetime.now() + timedelta(seconds=VERIFICATION_TIMEOUT)
        await add_pending_verification(user_id, chat_id, msg.message_id, verify_topic_id, expires_at)
        task = asyncio.create_task(
            verification_timer(user_id, chat_id, msg.message_id, verify_topic_id, VERIFICATION_TIMEOUT)
        )
        verification_tasks[(chat_id, user_id)] = task
        logger.info(f"Начата верификация для {user_id} в чате {chat_id}")

@router.callback_query(F.data.startswith("verify_"))
async def on_verify_button(callback: CallbackQuery):
    target_user_id = int(callback.data.split("_")[1])
    user = callback.from_user
    if user.id != target_user_id:
        return await callback.answer("Эта кнопка не для вас!", show_alert=True)
    if user.is_bot:
        return await callback.answer("Боты не могут верифицироваться.", show_alert=True)
    chat_id = callback.message.chat.id
    message_id = callback.message.message_id
    pending = await get_pending_verification(user.id, chat_id)
    if not pending or pending["message_id"] != message_id:
        return await callback.answer("Вы уже верифицированы или время истекло.", show_alert=True)
    task = verification_tasks.pop((chat_id, user.id), None)
    if task:
        task.cancel()
    await remove_pending_verification(user.id, chat_id)
    await delete_bot_message(chat_id, message_id)
    await callback.answer("Спасибо, вы верифицированы!")
    logger.info(f"Пользователь {user.id} верифицирован кнопкой в чате {chat_id}")

@router.message(F.text)
async def check_message_for_spam(message: Message):
    if message.from_user.is_bot:
        return
    user = message.from_user
    chat_id = message.chat.id
    user_id = user.id
    text = message.text or message.caption
    if not text:
        return

    pending = await get_pending_verification(user_id, chat_id)
    if pending:
        if AUTO_DELETE_UNVERIFIED:
            try:
                await message.delete()
            except TelegramAPIError as e:
                logger.error(f"Не удалось удалить сообщение неверифицированного {user_id}: {e}")
        return

    if not contains_spam(text):
        return

    # Проверка, нужно ли использовать LLM
    settings = await get_chat_settings(chat_id)
    if settings["use_llm"]:
        is_spam = await ask_llm(text)
        if not is_spam:
            logger.info(f"LLM решил, что сообщение не спам: {text[:100]}")
            return  # не спам, выходим

    mode = await get_effective_action_mode(chat_id)
    user_mention = user.mention_html()
    chat_title = message.chat.title or f"чат {chat_id}"

    if mode == "delete":
        try:
            await message.delete()
        except TelegramAPIError as e:
            logger.error(f"Не удалось удалить спам-сообщение {message.message_id}: {e}")
        await kick_user(chat_id, user_id)
        await add_banned_user(user_id, chat_id)
        await send_and_track(chat_id, f"Пользователь {user_mention} удалён за спам.",
                             message_thread_id=message.message_thread_id)
        logger.info(f"Спам-сообщение удалено, пользователь {user_id} забанен. Текст: {text[:100]}")
    elif mode == "notify_admin":
        if message.chat.type == "supergroup":
            try:
                msg_link = message.get_url()
            except:
                msg_link = "не удалось получить ссылку"
        else:
            msg_link = f"сообщение #{message.message_id}"
        warning_text = (
            f"@admin, обнаружен спам от пользователя {user_mention} в чате {chat_title}:\n"
            f"{text[:500]}\n\n"
            f"Сообщение: {msg_link}"
        )
        await send_and_track(chat_id, warning_text, parse_mode="HTML",
                             message_thread_id=message.message_thread_id)
        logger.info(f"Уведомление админов о спаме от {user_id}: {text[:100]}")

    logger.info(f"Спам-срабатывание: chat={chat_id}, user={user_id}, mode={mode}, text={text[:200]}")

# ─── Запуск ────────────────────────────────────────

async def main():
    global bot, llm_client

    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(key, None)

    session = AiohttpSession(timeout=60, limit=100)
    bot = Bot(token=BOT_TOKEN, session=session)

    # Инициализируем LLM клиент, если заданы ключи
    if LLM_API_KEY and LLM_ACCESS_ID:
        llm_client = AsyncOpenAI(
            api_key=LLM_API_KEY,
            base_url=f"https://agent.timeweb.cloud/api/v1/cloud-ai/agents/{LLM_ACCESS_ID}/v1",
            default_headers={"x-proxy-source": "bot"},
            timeout=LLM_TIMEOUT
        )
        logger.info("LLM клиент настроен")
    else:
        logger.warning("LLM не настроен (отсутствуют LLM_API_KEY или LLM_ACCESS_ID)")

    await init_db()
    logger.info("База данных инициализирована")
    await restart_verification_tasks()
    logger.info("Бот запущен")

    while True:
        try:
            await dp.start_polling(bot)
        except Exception as e:
            logger.error(f"Ошибка поллинга: {e}. Перезапуск через 5 секунд...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())