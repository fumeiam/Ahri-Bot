# features/send_catbox_multi.py
import asyncio
import aiohttp
import discord
import time
from core import personality

FEATURE_NAME = "SendCatboxMulti"
TRIGGERS = ["send"]

WAIT_FOR_DM_SECONDS = 180        # seconds to wait for user to upload
RATE_LIMIT_SECONDS = 60          # per-user cooldown for the command

_last_command_time = {}          # user_id -> last used timestamp

CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"
CATBOX_API_KEY = None            # optional: your catbox userhash


async def send_trigger_handler(bot, message: discord.Message, tokens):
    author = message.author
    orig_channel = message.channel

    try:
        # Rate limit per user
        now = time.time()
        last = _last_command_time.get(author.id, 0)
        if now - last < RATE_LIMIT_SECONDS:
            await orig_channel.send(f"{author.mention} Slow down~ try again later.")
            return
        _last_command_time[author.id] = now

        # DM the user
        try:
            dm = await author.create_dm()
            await dm.send(
                f"Hi~ upload your files for me to send in {orig_channel.mention}.\n"
                f"You have {WAIT_FOR_DM_SECONDS} seconds. You can upload multiple files at once."
            )
        except discord.Forbidden:
            await orig_channel.send(f"{author.mention} I can't DM you. Enable DMs from server members.")
            return

        # Wait for user attachments
        def check(m: discord.Message):
            return m.author.id == author.id and m.attachments and m.channel == dm

        try:
            user_msg: discord.Message = await bot.wait_for(
                "message", timeout=WAIT_FOR_DM_SECONDS, check=check
            )
        except asyncio.TimeoutError:
            await dm.send("Time's up~ No files received.")
            return

        # Process multiple attachments
        failed_files = []
        for attachment in user_msg.attachments:
            try:
                file_bytes = await attachment.read()
                catbox_url = await upload_to_catbox(file_bytes, attachment.filename)

                masked_url = f"[Video]({catbox_url})"
                await orig_channel.send(masked_url)

            except Exception as e:
                failed_files.append(f"{attachment.filename}: {e}")

        # Report results back to DM
        if failed_files:
            await dm.send(
                "Some files failed to upload:\n" + "\n".join(failed_files)
            )
        else:
            await dm.send("All done~ your files have been sent!")

    except Exception:
        try:
            await orig_channel.send(personality.ahri_say("oops"))
        except Exception:
            pass


async def upload_to_catbox(file_bytes: bytes, filename: str):
    """
    Upload a file to Catbox using aiohttp.FormData
    Returns the uploaded file URL.
    """
    data = aiohttp.FormData()
    data.add_field("reqtype", "fileupload")
    if CATBOX_API_KEY:
        data.add_field("userhash", CATBOX_API_KEY)
    data.add_field(
        "fileToUpload",
        file_bytes,
        filename=filename,
        content_type="application/octet-stream"
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(CATBOX_UPLOAD_URL, data=data) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise Exception(f"Catbox upload failed: {text}")
            return text.strip()


async def setup(bot: discord.Client):
    """
    Loader entrypoint: register trigger and feature info
    """
    bot.trigger_handlers["send"] = send_trigger_handler
    bot.feature_info[FEATURE_NAME] = {"triggers": TRIGGERS}
    print(f"{FEATURE_NAME}: Feature loaded successfully")
