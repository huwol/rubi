from __future__ import annotations

import os
import platform
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


def _parse_owner_ids() -> set[int]:
    ids: set[int] = set()
    for part in (os.getenv("OWNER_IDS") or os.getenv("OWNER_ID") or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


class Admin(commands.Cog):
    """운영 중 전체 봇을 끄지 않고 코그 단위로 관리하기 위한 관리자 명령어."""

    admin = app_commands.Group(name="admin", description="봇 관리자 명령어")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.owner_ids = _parse_owner_ids()

    def _allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.owner_ids:
            return True
        if interaction.guild and interaction.guild.owner_id == interaction.user.id:
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.manage_guild)

    async def _deny(self, interaction: discord.Interaction):
        await interaction.response.send_message("이 명령어는 봇 관리자만 사용할 수 있습니다.", ephemeral=True)

    def _ext_name(self, name: str) -> str:
        name = name.strip()
        if name.startswith("cogs."):
            return name
        return f"cogs.{name}"

    @admin.command(name="reload", description="특정 기능 코그만 다시 불러옵니다. 예: tickets, tempvoice")
    @app_commands.describe(extension="다시 불러올 코그 이름. 예: tickets, rolebuttons, tempvoice")
    async def reload(self, interaction: discord.Interaction, extension: str):
        if not self._allowed(interaction):
            return await self._deny(interaction)

        ext = self._ext_name(extension)
        try:
            await self.bot.reload_extension(ext)
            await interaction.response.send_message(f"✅ `{ext}` 리로드 완료", ephemeral=True)
        except commands.ExtensionNotLoaded:
            try:
                await self.bot.load_extension(ext)
                await interaction.response.send_message(f"✅ `{ext}`가 로드되어 있지 않아 새로 로드했습니다.", ephemeral=True)
            except Exception as exc:
                await interaction.response.send_message(f"❌ `{ext}` 로드 실패\n```{type(exc).__name__}: {exc}```", ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"❌ `{ext}` 리로드 실패\n```{type(exc).__name__}: {exc}```", ephemeral=True)

    @admin.command(name="sync", description="슬래시 명령어를 Discord에 동기화합니다.")
    @app_commands.describe(guild_only="현재 서버에만 빠르게 동기화할지 여부")
    async def sync(self, interaction: discord.Interaction, guild_only: Optional[bool] = True):
        if not self._allowed(interaction):
            return await self._deny(interaction)

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if guild_only and interaction.guild:
                guild_obj = discord.Object(id=interaction.guild.id)
                self.bot.tree.copy_global_to(guild=guild_obj)
                synced = await self.bot.tree.sync(guild=guild_obj)
                await interaction.followup.send(f"✅ 현재 서버 명령어 {len(synced)}개 동기화 완료", ephemeral=True)
            else:
                synced = await self.bot.tree.sync()
                await interaction.followup.send(f"✅ 글로벌 명령어 {len(synced)}개 동기화 완료", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ 동기화 실패\n```{type(exc).__name__}: {exc}```", ephemeral=True)

    @admin.command(name="status", description="봇 상태와 로드된 기능을 확인합니다.")
    async def status(self, interaction: discord.Interaction):
        if not self._allowed(interaction):
            return await self._deny(interaction)

        loaded = sorted(self.bot.extensions.keys())
        failed = getattr(self.bot, "failed_extensions", {})
        latency_ms = round(self.bot.latency * 1000)

        embed = discord.Embed(title="봇 상태", color=discord.Color.green())
        embed.add_field(name="지연 시간", value=f"{latency_ms}ms", inline=True)
        embed.add_field(name="서버 수", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="Python", value=platform.python_version(), inline=True)
        embed.add_field(name="로드된 코그", value="\n".join(f"• `{x}`" for x in loaded) or "없음", inline=False)
        if failed:
            embed.add_field(
                name="로드 실패 코그",
                value="\n".join(f"• `{k}`: {v}" for k, v in failed.items())[:1000],
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
