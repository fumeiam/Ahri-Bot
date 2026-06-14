import os, aiohttp, discord, asyncio
from discord.ext import commands

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# Pretty pink embed color (Ahri’s charm~)
AHRI_PINK = 0xFF69B4


async def setup(bot):
    bot.feature_info["transcribe"] = {
        "triggers": ["transcribe"],
        "description": "Transcribe voice messages using Deepgram 💫"
    }
    bot.trigger_handlers["transcribe"] = handle_transcribe


async def handle_transcribe(bot, message, tokens):
    """Command: ahri transcribe (must reply to a voice message)"""

    # must reply to a voice message
    if not message.reference:
        await message.channel.send("Mhm~ you need to **reply** to a voice message if you want me to listen, darling~ 💋")
        return

    try:
        ref_msg = await message.channel.fetch_message(message.reference.message_id)
    except Exception:
        await message.channel.send("I can’t seem to find the message you’re replying to... did it vanish into the spirit realm? ✨")
        return

    # check attachment
    if not ref_msg.attachments:
        await message.channel.send("That message doesn’t have any voice for me to hear~ 💫")
        return

    attachment = ref_msg.attachments[0]
    if not attachment.content_type or not attachment.content_type.startswith("audio"):
        await message.channel.send("Mmm~ I can only transcribe **voice messages**, not regular files, cutie~ 💋")
        return

    if not DEEPGRAM_API_KEY:
        await message.channel.send("Mmm~ I can’t quite hear without my magic token~ Set my `DEEPGRAM_API_KEY` first, sweetheart~ 💕")
        return

    try:
        # Show typing while fetching and transcribing
        async with message.channel.typing():
            # Download the audio file
            async with aiohttp.ClientSession() as session:
                async with session.get(attachment.url) as resp:
                    if resp.status != 200:
                        raise Exception("Failed to fetch audio")
                    audio_data = await resp.read()

            # Send to Deepgram
            headers = {
                "Authorization": f"Token {DEEPGRAM_API_KEY}",
                "Content-Type": attachment.content_type
            }

            async with aiohttp.ClientSession() as session:
                async with session.post("https://api.deepgram.com/v1/listen?model=general", headers=headers, data=audio_data) as resp:
                    if resp.status != 200:
                        raise Exception(f"Deepgram error: {resp.status}")
                    result = await resp.json()

            transcript = (
                result.get("results", {})
                .get("channels", [{}])[0]
                .get("alternatives", [{}])[0]
                .get("transcript", "")
            )

            if not transcript:
                await message.channel.send("Mmm~ my magic ears couldn’t quite catch that one... maybe too quiet? 💫")
                return

            # Split long transcripts into parts if needed
            chunks = [transcript[i:i + 2000] for i in range(0, len(transcript), 2000)]

            for i, chunk in enumerate(chunks, start=1):
                embed = discord.Embed(
                    title="🎧 Voice Transcription",
                    description=chunk.strip(),
                    color=AHRI_PINK
                )
                embed.set_footer(text=f"Requested by {message.author.display_name} • Ahri’s whisper~ 💋")
                if len(chunks) > 1:
                    embed.title += f" (Part {i}/{len(chunks)})"
                await message.channel.send(embed=embed)
                await asyncio.sleep(1)

    except Exception as e:
        await message.channel.send("Seems my magic ears couldn’t catch that one~ maybe next time, darling~ 💫")
        return
