from __future__ import annotations

import asyncio
import os
import traceback
from pathlib import Path
from typing import Iterable, List, Optional

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

COG_EXTENSIONS: tuple[str, ...] = (
    "cogs.admin",
    "cogs.rolebuttons",
    "cogs.tickets",
    "cogs.invites",
    "cogs.chatlog",
    "cogs.tempvoice",
    "cogs.voice_stats",
    "cogs.leveling",
    "cogs.member_counter",
    "cogs.welcome",
)


def locate_env() -> Optional[Path]:
    for base in (Path.cwd(), BASE_DIR):
        p = base.resolve()
        for _ in range(6):
            candidate = p / ".env"
            if candidate.is_file():
                return candidate
            if p.parent == p:
                break
            p = p.parent
    return None


def parse_int_list(raw: str | None) -> List[int]:
    result: List[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            result.append(int(part))
    return result


def truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


env_file = locate_env()
load_dotenv(env_file, override=True)

TOKEN = (os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or "").strip().strip('"').strip("'")
if not TOKEN:
    raise SystemExit(f"DISCORD_TOKEN을 찾지 못했습니다. .env 위치 확인: {env_file}")

GUILD_IDS = parse_int_list(os.getenv("SYNC_GUILD_IDS"))
AUTO_SYNC = truthy(os.getenv("AUTO_SYNC"), default=True)
CLEAR_GLOBAL_COMMANDS = truthy(os.getenv("CLEAR_GLOBAL_COMMANDS"), default=False)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True


class MongHwanBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.loaded_extensions: list[str] = []
        self.failed_extensions: dict[str, str] = {}

    async def setup_hook(self) -> None:
        for ext in COG_EXTENSIONS:
            try:
                await self.load_extension(ext)
                self.loaded_extensions.append(ext)
                print(f"[cog] loaded: {ext}")
            except Exception as exc:
                self.failed_extensions[ext] = f"{type(exc).__name__}: {exc}"
                print(f"[cog] failed: {ext} -> {type(exc).__name__}: {exc}")
                traceback.print_exc()

        if AUTO_SYNC:
            await self.sync_commands()
        else:
            print("[slash] AUTO_SYNC=false, startup sync skipped. Use /admin sync after manual sync is available.")

    async def sync_commands(self, guild_ids: Iterable[int] | None = None) -> None:
        target_ids = list(guild_ids if guild_ids is not None else GUILD_IDS)

        if target_ids:
            for gid in target_ids:
                guild_obj = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
                print(f"[slash] guild {gid} synced: {len(synced)} cmds")

            if CLEAR_GLOBAL_COMMANDS:
                self.tree.clear_commands(guild=None)
                cleared = await self.tree.sync()
                print(f"[slash] global commands cleared: {len(cleared)} cmds remain")
        else:
            synced = await self.tree.sync()
            print(f"[slash] global synced: {len(synced)} cmds")


bot = MongHwanBot()


@bot.event
async def on_ready():
    assert bot.user is not None
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Guilds: {len(bot.guilds)}")
    if bot.failed_extensions:
        print("[warning] failed extensions:")
        for ext, err in bot.failed_extensions.items():
            print(f" - {ext}: {err}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)

    if isinstance(error, app_commands.MissingPermissions):
        message = "이 명령어를 사용할 권한이 없습니다."
    elif isinstance(error, app_commands.BotMissingPermissions):
        message = "봇 권한이 부족해서 실행할 수 없습니다."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"잠시 후 다시 시도해 주세요. ({error.retry_after:.1f}초)"
    else:
        message = f"명령어 실행 중 오류가 발생했습니다.\n```{type(original).__name__}: {original}```"
        traceback.print_exception(type(original), original, getattr(original, "__traceback__", None))

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass


async def main():
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
