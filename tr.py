import requests
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from utils import modules_help, prefix

def google_translate(query, source_lang="auto", target_lang="en"):
    url = "https://translate.google.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": source_lang,
        "tl": target_lang,
        "dt": "t",
        "q": query
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    response = requests.get(url, params=params, headers=headers)
    if response.status_code == 200:
        response.encoding = "utf-8"
        data = response.json()
        return "".join([item[0] for item in data[0]])
    else:
        raise Exception("Failed to fetch translation.")

@Client.on_message(filters.command(["tr"], prefix))
async def translate_text(client, message: Message):
    args = message.text.split(maxsplit=2)
    reply_text = message.reply_to_message.text or message.reply_to_message.caption if message.reply_to_message else None
    if len(args) < 2 and not reply_text:
        usage_message = (
            f"<b>Usage:</b> <code>{prefix}gtr [language] [text]</code>\n"
        )
        await message.edit(usage_message) if message.from_user.is_self else await message.reply(usage_message)
        return

    target_lang = args[1] if len(args) > 1 else "en"
    query = args[2].strip() if len(args) > 2 else ""
    if not query and reply_text:
        query = reply_text.strip()

    if not query:
        await message.reply("No text found to translate.")
        return

    processing_message = await (message.edit("Translating...") if message.from_user.is_self else message.reply("Translating..."))

    try:
        translated_text = google_translate(query, target_lang=target_lang)
        await processing_message.edit(f"**Translated Text ({target_lang.upper()}):**\n{translated_text}", parse_mode=enums.ParseMode.MARKDOWN)
    except Exception as e:
        await processing_message.edit(f"Failed to translate the text: {str(e)}")

modules_help["translate"] = {
    "tr [language] [text]": "Translate the provided text to the specified language."
}
