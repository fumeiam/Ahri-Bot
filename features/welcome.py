# features/greetings.py
import discord
from discord.ext import commands
import logging
import json
import os
from typing import List

# Storage file for greetings settings
GREETINGS_FILE = "data/greetings.json"

def load_greetings_data():
    """Load greetings data from JSON file"""
    if not os.path.exists(GREETINGS_FILE):
        return {}
    
    try:
        with open(GREETINGS_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {}

def save_greetings_data(data):
    """Save greetings data to JSON file"""
    os.makedirs(os.path.dirname(GREETINGS_FILE), exist_ok=True)
    with open(GREETINGS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

async def setup(bot):
    await bot.add_cog(Greetings(bot))

class Greetings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Send welcome message when a member joins"""
        if member.bot:
            return
            
        guild_id = str(member.guild.id)
        greetings_data = load_greetings_data()
        guild_data = greetings_data.get(guild_id, {})
        
        # Check if greetings are enabled and channel is set
        if not guild_data.get("enabled", False):
            return
            
        channel_id = guild_data.get("channel")
        if not channel_id:
            return
            
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            logging.warning(f"Greetings channel {channel_id} not found in guild {guild_id}")
            return
            
        # Use the hardcoded welcome message
        welcome_message = f"Ah, I have been waiting for you {member.mention}; welcome to my den 💖~"
            
        try:
            await channel.send(welcome_message)
        except discord.Forbidden:
            logging.error(f"Missing permissions to send message in channel {channel_id}")
        except Exception as e:
            logging.exception(f"Failed to send welcome message: {e}")

async def greet_command(bot, message: discord.Message, tokens: List[str]):
    """Handle greet commands"""
    if not tokens:
        await message.channel.send(
            "**Greetings Commands:**\n"
            f"`{bot.command_prefix}greet set #channel` - Set welcome channel\n"
            f"`{bot.command_prefix}greet toggle on/off` - Enable/disable greetings\n"
            f"`{bot.command_prefix}greet test` - Test the welcome message"
        )
        return
        
    subcommand = tokens[0].lower()
    guild_id = str(message.guild.id)
    greetings_data = load_greetings_data()
    
    if guild_id not in greetings_data:
        greetings_data[guild_id] = {"enabled": False, "channel": None}
    
    if subcommand == "set" and message.channel_mentions:
        # Set greetings channel
        channel = message.channel_mentions[0]
        greetings_data[guild_id]["channel"] = str(channel.id)
        save_greetings_data(greetings_data)
        await message.channel.send(f"✨ I'll now welcome new members in {channel.mention}!")
        
    elif subcommand == "toggle" and len(tokens) > 1:
        # Enable/disable greetings
        state = tokens[1].lower()
        if state in ["on", "enable", "true"]:
            greetings_data[guild_id]["enabled"] = True
            save_greetings_data(greetings_data)
            await message.channel.send("✨ Greetings enabled! I'll welcome new members from now on.")
        elif state in ["off", "disable", "false"]:
            greetings_data[guild_id]["enabled"] = False
            save_greetings_data(greetings_data)
            await message.channel.send("✨ Greetings disabled. I won't welcome new members anymore.")
        else:
            await message.channel.send("❌ Please specify 'on' or 'off' to enable/disable greetings.")
            
    elif subcommand == "test":
        # Test the welcome message
        channel_id = greetings_data[guild_id].get("channel")
        
        if not channel_id:
            await message.channel.send("❌ You need to set a welcome channel first! Use `greet set #channel`")
            return
            
        channel = bot.get_channel(int(channel_id))
        if not channel:
            await message.channel.send("❌ The welcome channel seems to be missing. Please set a new one.")
            return
            
        # Use the hardcoded welcome message
        welcome_message = f"Ah, I have been waiting for you {message.author.mention}; welcome to my den 💖~"
        
        try:
            await channel.send(f"**Test welcome message:**\n{welcome_message}")
            await message.channel.send("✨ Test message sent to the welcome channel!")
        except discord.Forbidden:
            await message.channel.send("❌ I don't have permission to send messages in the welcome channel.")
        except Exception as e:
            await message.channel.send("❌ Something went wrong while sending the test message.")
            logging.exception(f"Failed to send test message: {e}")
            
    else:
        await message.channel.send("❌ I didn't understand that command. Use `greet` for help.")

# Mark this function as needing admin permissions
greet_command._needs_admin = True

# Register the command
def setup(bot):
    # Add the feature info
    bot.feature_info["greetings"] = {
        "name": "Greetings",
        "description": "Welcome new members with a custom message",
        "triggers": ["greet", "welcome"],
        "usage": f"{bot.command_prefix}greet set #channel\n{bot.command_prefix}greet toggle on/off"
    }
    
    # Register the command handler
    bot.trigger_handlers["greet"] = greet_command
    bot.trigger_handlers["welcome"] = greet_command
    
    # Add the cog
    import asyncio
    asyncio.create_task(bot.add_cog(Greetings(bot)))
