import asyncio
import os
import random
import time
from collections import defaultdict
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from utils.scripts import import_library
from utils.db import db
from utils import modules_help, prefix
from modules.custom_modules.elevenlabs import generate_elevenlabs_audio
from PIL import Image
import datetime
import pytz
import requests

genai = import_library("google.generativeai", "google-generativeai")
safety_settings = [
    {"category": cat, "threshold": "BLOCK_NONE"}
    for cat in [
        "HARM_CATEGORY_DANGEROUS_CONTENT",
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_UNSPECIFIED",
    ]
]
la_timezone = pytz.timezone("America/Los_Angeles")

generation_config = {
    "max_output_tokens": 40,
}

ROLES_URL = "https://gist.githubusercontent.com/iTahseen/00890d65192ca3bd9b2a62eb034b96ab/raw/roles.json"

history_collection = "custom.wchat"
settings_collection = "custom.wsettings"

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"
_gemini_model_cache = None

DEFAULT_HISTORY_HEAD = 50
DEFAULT_HISTORY_TAIL = 50

GEMINI_SEMAPHORE = asyncio.Semaphore(4)


def _sync_write_file(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


reply_queue = asyncio.Queue()
_reply_worker_task = None


async def reply_worker(client):
    while True:
        reply_func, args, kwargs = await reply_queue.get()
        cleanup_file = kwargs.pop("cleanup_file", None)
        try:
            try:
                await reply_func(*args, **kwargs)
            except FloodWait as e:
                try:
                    await client.send_message("me", f"⚠️ FloodWait\nSleeping {e.value}s")
                except Exception:
                    pass
                await asyncio.sleep(e.value + 1)
                await reply_func(*args, **kwargs)
        except Exception as e:
            try:
                await client.send_message("me", f"⚠️ Reply queue error\n{e}")
            except Exception:
                pass
        finally:
            if cleanup_file and os.path.exists(cleanup_file):
                try:
                    os.remove(cleanup_file)
                except Exception:
                    pass
        await asyncio.sleep(1)


def ensure_reply_worker(client):
    global _reply_worker_task
    if _reply_worker_task is None or _reply_worker_task.done():
        _reply_worker_task = asyncio.create_task(reply_worker(client))


async def send_reply(reply_func, args, kwargs, client):
    ensure_reply_worker(client)
    if isinstance(args, tuple):
        args = list(args)
    await reply_queue.put((reply_func, args, kwargs))


async def instant_reply(reply_func, args, kwargs, client):
    if isinstance(args, tuple):
        args = list(args)
    cleanup_file = kwargs.pop("cleanup_file", None)
    try:
        try:
            await reply_func(*args, **kwargs)
        except FloodWait as e:
            try:
                await client.send_message("me", f"⚠️ FloodWait\nSleeping {e.value}s")
            except Exception:
                pass
            await asyncio.sleep(e.value + 1)
            await reply_func(*args, **kwargs)
    finally:
        if cleanup_file and os.path.exists(cleanup_file):
            try:
                os.remove(cleanup_file)
            except Exception:
                pass


def get_gemini_model():
    global _gemini_model_cache
    if _gemini_model_cache is not None:
        return _gemini_model_cache
    model_name = db.get(settings_collection, "gemini_model") or DEFAULT_GEMINI_MODEL
    _gemini_model_cache = model_name
    return model_name


def set_gemini_model(model_name: str):
    global _gemini_model_cache
    db.set(settings_collection, "gemini_model", model_name)
    _gemini_model_cache = model_name


def get_voice_generation_enabled():
    enabled = db.get(settings_collection, "voice_generation_enabled")
    if enabled is None:
        enabled = True
        db.set(settings_collection, "voice_generation_enabled", True)
    return enabled


def set_voice_generation_enabled(enabled: bool):
    db.set(settings_collection, "voice_generation_enabled", enabled)


def get_history_limits():
    head = db.get(settings_collection, "history_head")
    tail = db.get(settings_collection, "history_tail")
    if not isinstance(head, int):
        try:
            head = int(head)
        except (TypeError, ValueError):
            head = DEFAULT_HISTORY_HEAD
    if not isinstance(tail, int):
        try:
            tail = int(tail)
        except (TypeError, ValueError):
            tail = DEFAULT_HISTORY_TAIL
    return head, tail


enabled_topics = db.get(settings_collection, "enabled_topics") or []
disabled_topics = db.get(settings_collection, "disabled_topics") or []
wchat_for_all_groups = db.get(settings_collection, "wchat_for_all_groups") or {}
group_roles = db.get(settings_collection, "group_roles") or {}

smileys = ["-.-", "):", ":)", "*.*", ")*"]

_roles_cache = None
_roles_cache_time = 0
ROLES_CACHE_TTL = 300  # seconds


def _fetch_roles_sync():
    r = requests.get(ROLES_URL, timeout=5)
    r.raise_for_status()
    return r.json()


async def fetch_roles():
    global _roles_cache, _roles_cache_time
    now = time.time()
    if _roles_cache is not None and (now - _roles_cache_time) < ROLES_CACHE_TTL:
        return _roles_cache
    try:
        roles = await asyncio.to_thread(_fetch_roles_sync)
        if isinstance(roles, dict):
            default_role_name = db.get(settings_collection, "default_role") or "default"
            if default_role_name in roles:
                roles["default"] = roles[default_role_name]
            _roles_cache = roles
            _roles_cache_time = now
            return roles
        return _roles_cache or {}
    except requests.exceptions.RequestException:
        return _roles_cache or {}
    except Exception:
        return _roles_cache or {}


def get_chat_history(topic_id, user_message, user_name):
    max_head, max_tail = get_history_limits()
    chat_history = db.get(history_collection, f"chat_history.{topic_id}") or []
    chat_history.append(f"{user_name}: {user_message}")
    db.set(history_collection, f"chat_history.{topic_id}", chat_history)
    if len(chat_history) > max_head + max_tail:
        return chat_history[:max_head] + ["..."] + chat_history[-max_tail:]
    return chat_history


def build_system_instruction(bot_role):
    if isinstance(bot_role, list):
        return "\n".join(bot_role)
    return str(bot_role)


def build_prompt(chat_history):
    timestamp = datetime.datetime.now(la_timezone).strftime("%Y-%m-%d %H:%M:%S")
    chat_context = "\n".join(chat_history)
    return f"Time: {timestamp}\nChat History:\n{chat_context}"


async def generate_gemini_response(input_data, chat_history, topic_id, bot_role=None):
    retries = 3
    gemini_keys = db.get(settings_collection, "gemini_keys")
    current_key_index = db.get(settings_collection, "current_key_index") or 0
    system_instruction = build_system_instruction(bot_role) if bot_role else None

    while retries > 0:
        try:
            current_key = gemini_keys[current_key_index]
            genai.configure(api_key=current_key)
            model = genai.GenerativeModel(
                get_gemini_model(),
                generation_config=generation_config,
                system_instruction=system_instruction,
            )
            model.safety_settings = safety_settings

            async with GEMINI_SEMAPHORE:
                response = await asyncio.to_thread(model.generate_content, input_data)
            text = getattr(response, "text", None)
            bot_response = text.strip() if isinstance(text, str) else ""

            if bot_response:
                full_history = db.get(history_collection, f"chat_history.{topic_id}") or []
                full_history.append(bot_response)
                db.set(history_collection, f"chat_history.{topic_id}", full_history)
            return bot_response
        except Exception as e:
            if "429" in str(e) or "invalid" in str(e).lower() or "403" in str(e) or "suspended" in str(e).lower():
                retries -= 1
                current_key_index = (current_key_index + 1) % len(gemini_keys)
                db.set(settings_collection, "current_key_index", current_key_index)
                await asyncio.sleep(4)
            else:
                raise e
    return ""


async def upload_file_to_gemini(file_path, file_type):
    uploaded_file = await asyncio.to_thread(genai.upload_file, file_path)
    while uploaded_file.state.name == "PROCESSING":
        await asyncio.sleep(10)
        uploaded_file = await asyncio.to_thread(genai.get_file, uploaded_file.name)
    if uploaded_file.state.name == "FAILED":
        raise ValueError(f"{file_type.capitalize()} failed to process.")
    return uploaded_file


async def handle_voice_message(client, chat_id, bot_response, thread_id=None):
    if not isinstance(bot_response, str) or ".el" not in bot_response:
        return False

    start_index = bot_response.find(".el")
    if start_index != -1:
        bot_response = bot_response[start_index + len(".el"):].strip()

    text_kwargs = {"message_thread_id": thread_id} if thread_id else {}

    if not get_voice_generation_enabled():
        await send_reply(client.send_message, [chat_id, bot_response], text_kwargs, client)
        return True

    try:
        audio_path = await generate_elevenlabs_audio(text=bot_response)
        if audio_path and os.path.exists(audio_path):
            kwargs = {"voice": audio_path, "cleanup_file": audio_path}
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            await send_reply(client.send_voice, [chat_id], kwargs, client)
        else:
            await send_reply(client.send_message, [chat_id, bot_response], text_kwargs, client)
    except Exception:
        await send_reply(client.send_message, [chat_id, bot_response], text_kwargs, client)
    return True


@Client.on_message(filters.sticker & filters.group & ~filters.me)
async def handle_sticker(client: Client, message: Message):
    try:
        group_id = str(message.chat.id)
        topic_id = f"{group_id}:{message.message_thread_id}"
        if topic_id in disabled_topics or (
            not wchat_for_all_groups.get(group_id, False)
            and topic_id not in enabled_topics
        ):
            return
        random_smiley = random.choice(smileys)
        await asyncio.sleep(random.uniform(5, 10))
        await send_reply(message.reply_text, [random_smiley], {}, client)
    except Exception as e:
        await send_reply(
            client.send_message,
            ["me", f"⚠️ handle_sticker error\n@{message.chat.username or message.chat.id}:{message.message_thread_id}\n{str(e)}"],
            {}, client,
        )


@Client.on_message(filters.animation & filters.group & ~filters.me)
async def handle_gif(client: Client, message: Message):
    try:
        group_id = str(message.chat.id)
        topic_id = f"{group_id}:{message.message_thread_id}"
        if topic_id in disabled_topics or (
            not wchat_for_all_groups.get(group_id, False) and topic_id not in enabled_topics
        ):
            return
        random_smiley = random.choice(smileys)
        await asyncio.sleep(random.uniform(5, 10))
        await send_reply(message.reply_text, [random_smiley], {}, client)
    except Exception as e:
        await send_reply(
            client.send_message,
            ["me", f"⚠️ handle_gif error\n@{message.chat.username or message.chat.id}:{message.message_thread_id}\n{str(e)}"],
            {}, client,
        )


topic_message_buffer = defaultdict(list)
topic_message_timers = {}
topic_last_message = {}


async def process_topic_buffer(client, topic_id):
    try:
        await asyncio.sleep(8)
        buffered = topic_message_buffer.pop(topic_id, [])
        last_message = topic_last_message.pop(topic_id, None)
        topic_message_timers.pop(topic_id, None)

        if not buffered or last_message is None:
            return

        group_id = str(last_message.chat.id)

        roles = await fetch_roles()
        default_role = roles.get("default")
        if not default_role:
            await send_reply(client.send_message, ["me", "⚠️ 'default' role missing."], {}, client)
            return

        bot_role = db.get(settings_collection, f"custom_roles.{topic_id}") or group_roles.get(group_id) or default_role

        chat_history = None
        for user_name, user_message in buffered:
            chat_history = get_chat_history(topic_id, user_message, user_name)

        await asyncio.sleep(random.choice([6, 8, 10]))

        prompt = build_prompt(chat_history)
        bot_response = await generate_gemini_response(prompt, chat_history, topic_id, bot_role=bot_role)

        if not bot_response:
            await send_reply(
                client.send_message,
                ["me", f"⚠️ Gemini empty response\n@{last_message.chat.username or last_message.chat.id}:{last_message.message_thread_id}"],
                {}, client,
            )
            return

        if await handle_voice_message(client, last_message.chat.id, bot_response, thread_id=last_message.message_thread_id):
            return

        await send_reply(
            client.send_message,
            [last_message.chat.id, bot_response],
            {"message_thread_id": last_message.message_thread_id},
            client,
        )
    except Exception as e:
        await send_reply(
            client.send_message,
            ["me", f"⚠️ wchat module error\n{topic_id}\n{str(e)}"],
            {}, client,
        )


@Client.on_message(filters.text & filters.group & ~filters.me)
async def wchat(client: Client, message: Message):
    try:
        group_id = str(message.chat.id)
        topic_id = f"{group_id}:{message.message_thread_id}"

        if message.from_user is None:
            user_name = "User"
        else:
            user_name = message.from_user.first_name or "User"

        user_message = message.text.strip()

        if topic_id in disabled_topics or (
            not wchat_for_all_groups.get(group_id, False) and topic_id not in enabled_topics
        ):
            return

        topic_message_buffer[topic_id].append((user_name, user_message))
        topic_last_message[topic_id] = message

        existing_timer = topic_message_timers.get(topic_id)
        if existing_timer:
            existing_timer.cancel()

        topic_message_timers[topic_id] = asyncio.create_task(process_topic_buffer(client, topic_id))
    except Exception as e:
        await send_reply(
            client.send_message,
            [
                "me",
                f"⚠️ wchat module error\n@{message.chat.username or message.chat.id}:{message.message_thread_id}\n{str(e)}",
            ],
            {}, client,
        )


image_buffer = defaultdict(list)
image_timers = {}


@Client.on_message(filters.group & ~filters.me)
async def handle_files(client: Client, message: Message):
    file_path = None
    try:
        group_id = str(message.chat.id)
        topic_id = f"{group_id}:{message.message_thread_id}"

        user_name = message.from_user.first_name if message.from_user else "User"

        if topic_id in disabled_topics or (
            not wchat_for_all_groups.get(group_id, False)
            and topic_id not in enabled_topics
        ):
            return

        if not any([
            message.photo, message.video, message.video_note,
            message.audio, message.voice, message.document,
        ]):
            return

        roles = await fetch_roles()
        default_role = roles.get("default")

        if not default_role:
            await send_reply(client.send_message, ["me", "⚠️ 'default' role missing."], {}, client)
            return

        bot_role = db.get(settings_collection, f"custom_roles.{topic_id}") or group_roles.get(group_id) or default_role

        caption = message.caption.strip() if message.caption else ""

        if message.photo:
            chat_history = get_chat_history(topic_id, caption or "[image]", user_name)
            chat_context = "\n".join(chat_history)

            image_path = await client.download_media(message.photo)
            image_buffer[topic_id].append(image_path)

            if image_timers.get(topic_id) is None:
                image_timers[topic_id] = True

                async def process_images():
                    try:
                        await asyncio.sleep(10)
                        image_paths = image_buffer.pop(topic_id, [])
                        image_timers[topic_id] = None

                        if not image_paths:
                            return

                        sample_images = []
                        try:
                            for img_path in image_paths:
                                try:
                                    img = await asyncio.to_thread(Image.open, img_path)
                                    sample_images.append(img)
                                except Exception:
                                    continue

                            if not sample_images:
                                await send_reply(client.send_message, ["me", "⚠️ No valid images to process."], {}, client)
                                return

                            prompt = (
                                f"{chat_context}\n\nUser has sent multiple images."
                                f"{' Caption: ' + caption if caption else ''} Generate a response based on the content of the images, and our chat context. "
                                "Always follow the bot role, and talk like a human."
                            )
                            input_data = [prompt] + sample_images
                            response = await generate_gemini_response(
                                input_data, chat_history, topic_id, bot_role=bot_role
                            )
                            if not response:
                                await send_reply(
                                    client.send_message,
                                    ["me", f"⚠️ Gemini empty response\n@{message.chat.username or message.chat.id}:{topic_id}"],
                                    {}, client,
                                )
                            elif not await handle_voice_message(client, message.chat.id, response, thread_id=message.message_thread_id):
                                await send_reply(message.reply_text, [response], {}, client)
                        finally:
                            for img in sample_images:
                                try:
                                    img.close()
                                except Exception:
                                    pass
                            for path in image_paths:
                                if os.path.exists(path):
                                    os.remove(path)
                    except Exception as e:
                        await send_reply(
                            client.send_message,
                            ["me", f"⚠️ process_images error\n@{message.chat.username or message.chat.id}:{topic_id}\n{str(e)}"],
                            {}, client,
                        )

                asyncio.create_task(process_images())
            return

        file_type = None

        if message.video or message.video_note:
            file_type, file_path = (
                "video",
                await client.download_media(message.video or message.video_note),
            )
        elif message.audio or message.voice:
            file_type, file_path = (
                "audio",
                await client.download_media(message.audio or message.voice),
            )
        elif message.document and message.document.file_name.endswith(".pdf"):
            file_type, file_path = "pdf", await client.download_media(message.document)
        elif message.document:
            file_type, file_path = (
                "document",
                await client.download_media(message.document),
            )

        if file_path and file_type:
            chat_history = get_chat_history(topic_id, caption or f"[{file_type}]", user_name)
            chat_context = "\n".join(chat_history)

            uploaded_file = await upload_file_to_gemini(file_path, file_type)
            prompt = (
                f"{chat_context}\n\nUser has sent a {file_type}."
                f"{' Caption: ' + caption if caption else ''} Generate a response based on the content of the {file_type}, and our chat context, always follow role."
            )
            input_data = [prompt, uploaded_file]
            response = await generate_gemini_response(
                input_data, chat_history, topic_id, bot_role=bot_role
            )
            if not response:
                await send_reply(
                    client.send_message,
                    ["me", f"⚠️ Gemini empty response\n@{message.chat.username or message.chat.id}:{topic_id}"],
                    {}, client,
                )
            elif not await handle_voice_message(client, message.chat.id, response, thread_id=message.message_thread_id):
                await send_reply(message.reply_text, [response], {}, client)
    except Exception as e:
        await send_reply(
            client.send_message,
            ["me", f"⚠️ handle_files error\n@{message.chat.username or message.chat.id}:{message.message_thread_id}\n{str(e)}"],
            {}, client,
        )
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


@Client.on_message(filters.command(["wchat", "wc"], prefix) & filters.me)
async def wchat_command(client: Client, message: Message):
    try:
        parts = message.text.strip().split()
        group_id = str(message.chat.id)

        if len(parts) < 2:
            await instant_reply(message.edit_text, [f"Usage: {prefix}wchat [on|off|del|all|r] [topic_id]"], {}, client)
            return

        if len(parts) == 2:
            topic_id = f"{group_id}:{message.message_thread_id}"
            command = parts[1].lower()
        else:
            topic_id = f"{group_id}:{parts[1]}"
            command = parts[2].lower()

        if command == "on":
            if topic_id in disabled_topics:
                disabled_topics.remove(topic_id)
                db.set(settings_collection, "disabled_topics", disabled_topics)
            if topic_id not in enabled_topics:
                enabled_topics.append(topic_id)
                db.set(settings_collection, "enabled_topics", enabled_topics)
            await instant_reply(message.edit_text, [f"<spoiler>ON: {topic_id}</spoiler>"], {}, client)

        elif command == "off":
            if topic_id not in disabled_topics:
                disabled_topics.append(topic_id)
                db.set(settings_collection, "disabled_topics", disabled_topics)
            if topic_id in enabled_topics:
                enabled_topics.remove(topic_id)
                db.set(settings_collection, "enabled_topics", enabled_topics)
            await instant_reply(message.edit_text, [f"<spoiler>OFF: {topic_id}</spoiler>"], {}, client)

        elif command == "del":
            db.set(history_collection, f"chat_history.{topic_id}", None)
            await instant_reply(message.edit_text, [f"<spoiler>Deleted: {topic_id}</spoiler>"], {}, client)

        elif command == "all":
            wchat_for_all_groups[group_id] = not wchat_for_all_groups.get(group_id, False)
            db.set(settings_collection, "wchat_for_all_groups", wchat_for_all_groups)
            await instant_reply(
                message.edit_text,
                [f"All: {'enabled' if wchat_for_all_groups[group_id] else 'disabled'}"],
                {}, client,
            )

        elif command == "r":
            changed = False
            if topic_id in enabled_topics:
                enabled_topics.remove(topic_id)
                db.set(settings_collection, "enabled_topics", enabled_topics)
                changed = True
            if topic_id in disabled_topics:
                disabled_topics.remove(topic_id)
                db.set(settings_collection, "disabled_topics", disabled_topics)
                changed = True
            await instant_reply(
                message.edit_text,
                [f"<spoiler>Removed: {topic_id}</spoiler>" if changed else f"<spoiler>Not found: {topic_id}</spoiler>"],
                {}, client,
            )

        else:
            await instant_reply(message.edit_text, [f"Usage: {prefix}wchat [on|off|del|all|r] [topic_id]"], {}, client)

        await instant_reply(message.delete, [], {}, client)
    except Exception as e:
        await instant_reply(
            client.send_message,
            ["me", f"⚠️ wchat command error\n@{message.chat.username or message.chat.id}\n{str(e)}"],
            {}, client,
        )


@Client.on_message(filters.command("wrole", prefix) & filters.me)
async def set_custom_role(client: Client, message: Message):
    try:
        parts = message.text.strip().split()
        if len(parts) < 2:
            await instant_reply(message.edit_text, [f"Usage: {prefix}wrole [group|topic] <custom role>"], {}, client)
            return

        scope = parts[1].lower()
        group_id = str(message.chat.id)
        roles = await fetch_roles()
        default_role = roles.get("default")

        if not default_role:
            await instant_reply(client.send_message, ["me", "⚠️ 'default' role missing."], {}, client)
            return

        if scope == "group":
            if len(parts) == 2:
                group_roles.pop(group_id, None)
                db.set(settings_collection, "group_roles", group_roles)
                await instant_reply(message.edit_text, [f"<spoiler>Role reset: {group_id}</spoiler>"], {}, client)
            else:
                custom_role = " ".join(parts[2:]).strip()
                group_roles[group_id] = custom_role
                db.set(settings_collection, "group_roles", group_roles)
                await instant_reply(
                    message.edit_text, [f"<spoiler>Role set: {group_id}</spoiler>\n{custom_role}"], {}, client
                )
        elif scope == "topic":
            if len(parts) == 2:
                topic_id = f"{group_id}:{message.message_thread_id}"
                db.set(settings_collection, f"custom_roles.{topic_id}", default_role)
                db.set(history_collection, f"chat_history.{topic_id}", None)
                await instant_reply(message.edit_text, [f"<spoiler>Role reset: {topic_id}</spoiler>"], {}, client)
            elif len(parts) == 3:
                topic_id = f"{group_id}:{parts[2]}"
                group_role = group_roles.get(group_id, default_role)
                db.set(settings_collection, f"custom_roles.{topic_id}", group_role)
                db.set(history_collection, f"chat_history.{topic_id}", None)
                await instant_reply(
                    message.edit_text, [f"<spoiler>Role reset to group: {topic_id}</spoiler>"], {}, client
                )
            else:
                if parts[2].isdigit():
                    topic_id = f"{group_id}:{parts[2]}"
                    custom_role = " ".join(parts[3:]).strip()
                else:
                    topic_id = f"{group_id}:{message.message_thread_id}"
                    custom_role = " ".join(parts[2:]).strip()
                db.set(settings_collection, f"custom_roles.{topic_id}", custom_role)
                db.set(history_collection, f"chat_history.{topic_id}", None)
                await instant_reply(
                    message.edit_text, [f"<spoiler>Role set: {topic_id}</spoiler>\n{custom_role}"], {}, client
                )
        else:
            await instant_reply(message.edit_text, ["Invalid scope. Use 'group' or 'topic'."], {}, client)

        await instant_reply(message.delete, [], {}, client)
    except Exception as e:
        await instant_reply(
            client.send_message,
            ["me", f"⚠️ wrole command error\n@{message.chat.username or message.chat.id}\n{str(e)}"],
            {}, client,
        )


@Client.on_message(filters.command("wswitch", prefix) & filters.me)
async def switch_role(client: Client, message: Message):
    try:
        roles = await fetch_roles()
        if not roles:
            await instant_reply(client.send_message, ["me", "⚠️ Role fetch error."], {}, client)
            await instant_reply(message.edit_text, ["Failed to fetch roles."], {}, client)
            return

        parts = message.text.strip().split()
        group_id = str(message.chat.id)

        if len(parts) == 1:
            available_roles = "\n".join([f"- {role}" for role in roles.keys()])
            await instant_reply(message.edit_text, [f"Roles:\n{available_roles}"], {}, client)
            return

        if len(parts) == 2:
            topic_id = f"{group_id}:{message.message_thread_id}"
            role_name = parts[1].lower()
        else:
            topic_id = f"{group_id}:{parts[1]}"
            role_name = parts[2].lower()

        if role_name in roles:
            db.set(settings_collection, f"custom_roles.{topic_id}", roles[role_name])
            db.set(history_collection, f"chat_history.{topic_id}", None)
            await instant_reply(
                message.edit_text, [f"<spoiler>Switched: {topic_id}</spoiler>\n{role_name}"], {}, client
            )
        else:
            await instant_reply(message.edit_text, [f"Not found: {role_name}"], {}, client)

        await instant_reply(message.delete, [], {}, client)
    except Exception as e:
        await instant_reply(
            client.send_message,
            ["me", f"⚠️ wswitch command error\n@{message.chat.username or message.chat.id}\n{str(e)}"],
            {}, client,
        )


@Client.on_message(filters.command(["setwchat", "setwc"], prefix) & filters.me)
async def set_gemini_key(client: Client, message: Message):
    try:
        command = message.text.strip().split()
        subcommand = command[1] if len(command) > 1 else None
        key = command[2] if len(command) > 2 else None

        gemini_keys = db.get(settings_collection, "gemini_keys") or []
        current_key_index = db.get(settings_collection, "current_key_index") or 0

        if subcommand == "model":
            if key:
                set_gemini_model(key)
                await instant_reply(message.edit_text, [f"Gemini model set to: {key}"], {}, client)
            else:
                await instant_reply(message.edit_text, [f"Current Gemini model: {get_gemini_model()}"], {}, client)
            return

        if subcommand == "voice":
            enabled = not get_voice_generation_enabled()
            set_voice_generation_enabled(enabled)
            stat = "ON" if enabled else "OFF"
            await instant_reply(message.edit_text, [f"Voice: {stat}"], {}, client)
            return

        if subcommand == "add" and key:
            if key in gemini_keys:
                await instant_reply(message.edit_text, ["Key already added!"], {}, client)
                return
            gemini_keys.append(key)
            db.set(settings_collection, "gemini_keys", gemini_keys)
            await instant_reply(message.edit_text, ["Gemini key added!"], {}, client)
            return

        if subcommand == "set" and key:
            index = int(key) - 1
            if 0 <= index < len(gemini_keys):
                current_key_index = index
                db.set(settings_collection, "current_key_index", current_key_index)
                genai.configure(api_key=gemini_keys[current_key_index])
                model = genai.GenerativeModel(get_gemini_model(), generation_config=generation_config)
                model.safety_settings = safety_settings
                await instant_reply(message.edit_text, [f"Current key set to: {key}"], {}, client)
            else:
                await instant_reply(message.edit_text, [f"Invalid key index: {key}"], {}, client)
            return

        if subcommand == "del" and key:
            index = int(key) - 1
            if 0 <= index < len(gemini_keys):
                del gemini_keys[index]
                db.set(settings_collection, "gemini_keys", gemini_keys)
                if current_key_index >= len(gemini_keys):
                    current_key_index = max(0, len(gemini_keys) - 1)
                    db.set(settings_collection, "current_key_index", current_key_index)
                await instant_reply(message.edit_text, [f"Key {key} deleted!"], {}, client)
            else:
                await instant_reply(message.edit_text, [f"Invalid key index: {key}"], {}, client)
            return

        if subcommand == "role":
            roles = await fetch_roles()
            if key:
                role_name = key.lower()
                if role_name in roles:
                    db.set(settings_collection, "default_role", role_name)
                    await instant_reply(message.edit_text, [f"Default: {role_name}"], {}, client)
                else:
                    await instant_reply(message.edit_text, [f"Not found: {role_name}"], {}, client)
            else:
                roles_list = "\n".join([f"- {r}" for r in roles.keys()]) if roles else "No roles found."
                current_default = db.get(settings_collection, "default_role") or "default"
                await instant_reply(
                    message.edit_text,
                    [f"Default role: {current_default}\n\nAvailable roles:\n{roles_list}"],
                    {}, client,
                )
            return

        if subcommand == "history":
            if key and key.isdigit():
                n = int(key)
                db.set(settings_collection, "history_head", n)
                db.set(settings_collection, "history_tail", n)
                await instant_reply(message.edit_text, [f"History head/tail set to: {n}"], {}, client)
                return
            elif len(command) > 3 and command[2].isdigit() and command[3].isdigit():
                head, tail = int(command[2]), int(command[3])
                db.set(settings_collection, "history_head", head)
                db.set(settings_collection, "history_tail", tail)
                await instant_reply(message.edit_text, [f"History head: {head}, tail: {tail}"], {}, client)
                return
            else:
                head, tail = get_history_limits()
                await instant_reply(
                    message.edit_text,
                    [
                        f"Current history head: {head}, tail: {tail}\n\n"
                        f"Usage: {prefix}setwc history <n> or {prefix}setwc history <head> <tail>"
                    ],
                    {}, client,
                )
                return

        keys_list = "\n".join([f"{i + 1}. {k}" for i, k in enumerate(gemini_keys)])
        current_key = gemini_keys[current_key_index] if gemini_keys else "None"
        voice_status = "ON" if get_voice_generation_enabled() else "OFF"
        head, tail = get_history_limits()
        current_default_role = db.get(settings_collection, "default_role") or "default"
        menu_text = (
            f"Keys:\n{keys_list}\n\n"
            f"Current: {current_key}\nModel: {get_gemini_model()}\n"
            f"Voice: {voice_status}\nRole: {current_default_role}\n"
            f"History head: {head}, tail: {tail}"
        )
        CHUNK_SIZE = 3800
        if len(menu_text) > CHUNK_SIZE:
            fp = f"wchat_menu_{int(time.time())}.txt"
            await asyncio.to_thread(_sync_write_file, fp, menu_text)
            await instant_reply(
                client.send_document,
                [message.chat.id, fp],
                {"caption": "wchat menu", "cleanup_file": fp},
                client,
            )
        else:
            await instant_reply(message.edit_text, [menu_text], {}, client)
    except Exception as e:
        await instant_reply(
            client.send_message,
            ["me", f"⚠️ setwchat error\n@{message.chat.username or message.chat.id}\n{str(e)}"],
            {}, client,
        )


@Client.on_message(filters.command("wtest", prefix) & filters.me)
async def test_wchat_keys(client: Client, message: Message):
    try:
        await instant_reply(message.edit_text, ["Testing Gemini keys..."], {}, client)
        gemini_keys = db.get(settings_collection, "gemini_keys") or []
        if not gemini_keys:
            await instant_reply(message.edit_text, ["No Gemini keys configured."], {}, client)
            return

        test_prompt = "ping"
        result_lines = [
            "Gemini API Key Test Results (wchat)\n",
            f"Model: {get_gemini_model()}\n",
            "-" * 40,
        ]
        for idx, key in enumerate(gemini_keys):
            try:
                genai.configure(api_key=key)
                test_model = genai.GenerativeModel(
                    get_gemini_model(), generation_config=generation_config
                )
                test_model.safety_settings = safety_settings
                async with GEMINI_SEMAPHORE:
                    response = await asyncio.to_thread(test_model.generate_content, test_prompt)
                text = getattr(response, "text", None)
                status = "OK" if text else "No response"
            except Exception as e:
                status = f"ERROR: {e.__class__.__name__}: {str(e)[:80]}"
            result_lines.append(f"{idx + 1}. {key[:10]}... → {status}")

        result_text = "\n".join(result_lines)
        file_path = "wchat_test_results.txt"
        await asyncio.to_thread(_sync_write_file, file_path, result_text)
        await instant_reply(
            client.send_document,
            [message.chat.id],
            {
                "document": file_path,
                "caption": "✅ Gemini API key test results (wchat)",
                "cleanup_file": file_path,
            },
            client,
        )
        await instant_reply(message.delete, [], {}, client)
    except Exception as e:
        await instant_reply(
            client.send_message,
            ["me", f"⚠️ wtest command error\n@{message.chat.username or message.chat.id}\n{str(e)}"],
            {}, client,
        )


modules_help["wchat"] = {
    "wchat on/off/del/all/r [topic_id]": "Manage wchat for topics.",
    "wrole [group|topic] <role>": "Set or reset group/topic role.",
    "wswitch [topic_id] <role>": "Show or set wchat roles.",
    "setwchat add/set/del <key|index>": "Manage Gemini API keys.",
    "setwchat": "Show Gemini config & status.",
    "setwchat model <n>": "Set/show Gemini model.",
    "setwchat voice": "Toggle voice reply.",
    "setwchat role <n>": "Set/show global role.",
    "setwchat history <n>": "Set/show chat history head/tail.",
    "wtest": "Test Gemini keys.",
}
