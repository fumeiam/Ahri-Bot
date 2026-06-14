#!/usr/bin/env python3
import re, asyncio, logging
import json, os, time
from dotenv import load_dotenv
from typing import Dict, Callable, Any, List

import discord
from discord import app_commands
from discord.ext import commands

from core import config, db, loader, personality, permissions, utils

INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.members = True
INTENTS.messages = True
INTENTS.message_content = True

TRIGGER = "ahri"
load_dotenv()


# ==============================
# MasterControl System
# ==============================

MASTER_PASSWORD = os.getenv("MASTER_PASSWORD")

if not MASTER_PASSWORD:
    raise RuntimeError(
        "MASTER_PASSWORD missing in .env"
    )

MASTER_FILE = "data/mastercontrol.json"
ATTEMPT_FILE = "data/master_attempts.json"

MAX_ATTEMPTS = 2
LOCK_TIME = 3600


def load_masters():
    if not os.path.exists(MASTER_FILE):
        return []

    with open(MASTER_FILE, "r") as f:
        return json.load(f)


def save_master(user_id: int):

    os.makedirs("data", exist_ok=True)

    masters = load_masters()

    if user_id not in masters:
        masters.append(user_id)

    with open(MASTER_FILE, "w") as f:
        json.dump(masters, f, indent=4)



def load_attempts():

    if not os.path.exists(ATTEMPT_FILE):
        return {}

    with open(ATTEMPT_FILE, "r") as f:
        return json.load(f)



def save_attempts(data):

    os.makedirs("data", exist_ok=True)

    with open(ATTEMPT_FILE, "w") as f:
        json.dump(data, f, indent=4)



def is_locked(guild_id, user_id):

    attempts = load_attempts()

    key = f"{guild_id}:{user_id}"

    data = attempts.get(key)

    if not data:
        return False, 0


    if data["count"] >= MAX_ATTEMPTS:

        passed = time.time() - data["time"]

        if passed < LOCK_TIME:

            remaining = int(
                (LOCK_TIME - passed) / 60
            )

            return True, remaining


        del attempts[key]
        save_attempts(attempts)


    return False, 0



def add_failed_attempt(guild_id, user_id):

    attempts = load_attempts()

    key = f"{guild_id}:{user_id}"


    if key not in attempts:

        attempts[key] = {
            "count": 1,
            "time": time.time()
        }

    else:

        attempts[key]["count"] += 1
        attempts[key]["time"] = time.time()


    save_attempts(attempts)



def clear_attempts(guild_id, user_id):

    attempts = load_attempts()

    key = f"{guild_id}:{user_id}"

    if key in attempts:
        del attempts[key]
        save_attempts(attempts)



class AhriBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or(TRIGGER + " "),
            intents=INTENTS,
            help_command=None,
            case_insensitive=True,
        )
        self.trigger_handlers: Dict[str, Callable] = {}
        self.feature_info: Dict[str, Dict[str, Any]] = {}
        self.failed_modules: List[str] = []

    async def setup_hook(self):
        # configure logging already done by import
        await loader.load_features(self)
        try:
            await self.tree.sync()
        except Exception as e:
            logging.exception("Slash sync failed: %s", e)

    async def on_ready(self):
        logging.getLogger().info("Ready as %s (%s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Game(name="with nine tails ✨"))

    async def on_guild_join(self, guild: discord.Guild):
        await db.ensure_guild(guild.id)

    async def on_message(self, message: discord.Message):
        # ignore bots & DMs
        if message.author.bot or message.guild is None:
            return

        content = (message.content or "").strip()
        m = re.match(rf'(?i)^\s*{re.escape(TRIGGER)}\b', content)
        if not m:
            # allow other commands (if any)
            try:
                await self.process_commands(message)
            except Exception:
                pass
            return

        rest = content[m.end():].strip()
        if not rest:
            try:
                await message.channel.send(personality.ahri_say("help_intro"))
            except Exception:
                pass
            return

        tokens = utils.tokenize(rest)
        if not tokens:
            await message.channel.send(personality.ahri_say("unknown_trigger", cmd=rest.split()[0] if rest else ""))
            return

        # activation gate
        guild_id = message.guild.id
        g = await db.load_guild(guild_id)
        if not g.get("activated", False):
            await message.channel.send(personality.ahri_say("inactive_hint"))
            return

        cmd = tokens.pop(0).lower()
        handler = self.trigger_handlers.get(cmd)
        if not handler:
            await message.channel.send(personality.ahri_say("unknown_trigger", cmd=cmd))
            return

        try:
            needs_admin = getattr(handler, "_needs_admin", False)
            if needs_admin and not await permissions.is_guild_admin(message.author, guild_id):
                await message.channel.send(personality.ahri_say("no_permission"))
                return
            await handler(self, message, tokens)
        except Exception as e:
            logging.exception("Trigger handler error for %s: %s", cmd, e)
            try:
                await message.channel.send(personality.ahri_say("oops"))
            except Exception:
                pass
        finally:
            try:
                await self.process_commands(message)
            except Exception:
                pass


bot = AhriBot()


# ==============================
# MasterControl Slash Command
# ==============================

class MasterControlModal(
    discord.ui.Modal,
    title="MasterControl Access"
):

    password = discord.ui.TextInput(
        label="Enter Master Password",
        required=True,
        style=discord.TextStyle.short
    )


    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        locked, remaining = is_locked(
            interaction.guild_id,
            interaction.user.id
        )


        if locked:

            await interaction.response.send_message(
                f"🔒 Too many attempts. Try again in {remaining} minutes.",
                ephemeral=True
            )

            return


        if self.password.value == MASTER_PASSWORD:

            save_master(interaction.user.id)

            clear_attempts(
                interaction.guild_id,
                interaction.user.id
            )

            await interaction.response.send_message(
                "✨ MasterControl unlocked.",
                ephemeral=True
            )


        else:

            add_failed_attempt(
                interaction.guild_id,
                interaction.user.id
            )

            await interaction.response.send_message(
                "❌ Incorrect password.",
                ephemeral=True
            )



@bot.tree.command(
    name="mastercontrol",
    description="Unlock AhriBot MasterControl"
)
async def mastercontrol(
    interaction: discord.Interaction
):

    await interaction.response.send_modal(
        MasterControlModal()
    )



# Slash: activate, deactivate, help
@bot.tree.command(name="activate", description="Activate AhriBot features for this server (admin-only)")
@app_commands.checks.has_permissions(administrator=True)
async def activate(interaction: discord.Interaction):
    await db.set_activated(interaction.guild_id, True)
    await permissions.ensure_owner_admin(interaction.guild)
    await interaction.response.send_message(personality.ahri_say("activated"), ephemeral=True)

@bot.tree.command(name="deactivate", description="Deactivate AhriBot features for this server (admin-only)")
@app_commands.checks.has_permissions(administrator=True)
async def deactivate(interaction: discord.Interaction):
    await db.set_activated(interaction.guild_id, False)
    await interaction.response.send_message(personality.ahri_say("deactivated"), ephemeral=True)

@bot.tree.command(name="help", description="Show AhriBot features and trigger commands")
async def help_cmd(interaction: discord.Interaction):
    info_lines = ["**Slash commands**: `/activate`, `/deactivate`, `/help`"]
    if bot.feature_info:
        info_lines.append("**Features loaded:**")
        for name, meta in bot.feature_info.items():
            triggers = ", ".join(meta.get("triggers", [])) or "—"
            info_lines.append(f"• **{name}** → `{triggers}`")
    if bot.failed_modules:
        info_lines.append("⚠️ Failed modules: " + ", ".join(bot.failed_modules))
    await interaction.response.send_message(personality.ahri_say("help_intro") + "\n" + "\n".join(info_lines), ephemeral=True)


def main():
    cfg = config.load_env()
    config.ensure_data_dir()
    bot.run(cfg.token)


if __name__ == "__main__":
    main()
