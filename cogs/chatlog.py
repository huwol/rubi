from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import DATA_DIR, load_json, save_json

CHATLOG_STORAGE = DATA_DIR / "chatlog_config.json"

LOG_CATEGORIES: dict[str, str] = {
    "message_delete": "메시지 삭제",
    "message_edit": "메시지 수정",
    "member_join_leave": "멤버 입장/퇴장",
    "member_update": "닉네임/역할 변경",
    "channel_update": "채널 생성/삭제/수정",
    "voice_update": "음성방 입장/퇴장/이동",
}

DEFAULT_FLAGS: dict[str, bool] = {key: True for key in LOG_CATEGORIES}

CATEGORY_CHOICES = [
    app_commands.Choice(name=name, value=key)
    for key, name in LOG_CATEGORIES.items()
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cut(text: str | None, limit: int = 1000) -> str:
    if not text:
        return "내용 없음"
    text = text.strip()
    if not text:
        return "내용 없음"
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "... (길이 초과)"


def _name(obj: Any) -> str:
    value = getattr(obj, "mention", None)
    if value:
        return value
    value = getattr(obj, "name", None)
    if value:
        return str(value)
    return str(obj)


def _id(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ChatLogCog(commands.Cog):
    """서버 로그/채팅 로그 코그."""

    log = app_commands.Group(name="log", description="서버 로그 채널을 설정합니다.")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[str, dict[str, Any]] = load_json(CHATLOG_STORAGE, {})

    # ---------- config ----------
    def _guild_config(self, guild_id: int) -> dict[str, Any]:
        cfg = self.config.setdefault(str(guild_id), {})
        cfg.setdefault("enabled", False)
        cfg.setdefault("channel_id", None)  # 기본/예비 로그 채널
        cfg.setdefault("category_channels", {})  # 항목별 로그 채널
        cfg.setdefault("flags", DEFAULT_FLAGS.copy())

        for key, value in DEFAULT_FLAGS.items():
            cfg["flags"].setdefault(key, value)

        # 이전 버전에서 잘못된 값이 들어갔을 때 방어
        if not isinstance(cfg.get("category_channels"), dict):
            cfg["category_channels"] = {}
        if not isinstance(cfg.get("flags"), dict):
            cfg["flags"] = DEFAULT_FLAGS.copy()

        return cfg

    def _save(self) -> None:
        save_json(CHATLOG_STORAGE, self.config)

    def _target_channel_id(self, guild_id: int, category: str) -> Optional[int]:
        cfg = self._guild_config(guild_id)
        category_channels = cfg.get("category_channels", {})

        category_channel_id = _id(category_channels.get(category))
        if category_channel_id:
            return category_channel_id

        return _id(cfg.get("channel_id"))

    def _all_log_channel_ids(self, guild_id: int) -> set[int]:
        cfg = self._guild_config(guild_id)
        ids: set[int] = set()

        default_channel_id = _id(cfg.get("channel_id"))
        if default_channel_id:
            ids.add(default_channel_id)

        category_channels = cfg.get("category_channels", {})
        if isinstance(category_channels, dict):
            for value in category_channels.values():
                channel_id = _id(value)
                if channel_id:
                    ids.add(channel_id)

        return ids

    def _flag_enabled(self, guild_id: int, category: str) -> bool:
        cfg = self._guild_config(guild_id)
        return (
            bool(cfg.get("enabled"))
            and bool(self._target_channel_id(guild_id, category))
            and bool(cfg.get("flags", {}).get(category, True))
        )

    async def _get_log_channel(
        self,
        guild: discord.Guild,
        category: str,
    ) -> Optional[discord.abc.Messageable]:
        if not self._flag_enabled(guild.id, category):
            return None

        channel_id = self._target_channel_id(guild.id, category)
        if not channel_id:
            return None

        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None

        if isinstance(channel, discord.abc.Messageable):
            return channel
        return None

    async def _send_log(self, guild: discord.Guild, category: str, embed: discord.Embed) -> None:
        channel = await self._get_log_channel(guild, category)
        if channel is None:
            return

        embed.timestamp = _now()
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[chatlog] send failed: guild={guild.id}, category={category}, {type(exc).__name__}: {exc}")

    def _base_embed(self, title: str, color: discord.Color) -> discord.Embed:
        return discord.Embed(title=title, color=color)

    def _category_channel_mention(self, guild: discord.Guild, category: str) -> str:
        cfg = self._guild_config(guild.id)
        category_channels = cfg.get("category_channels", {})
        channel_id = _id(category_channels.get(category))
        if not channel_id:
            return "기본 채널 사용"

        channel = guild.get_channel(channel_id)
        return channel.mention if channel else f"알 수 없는 채널 (`{channel_id}`)"

    # ---------- slash commands ----------
    @log.command(name="set", description="기본 로그 채널을 설정하고 로그 기능을 켭니다.")
    @app_commands.describe(channel="특정 항목 전용 채널이 없을 때 사용할 기본 로그 채널")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def log_set(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return

        cfg = self._guild_config(interaction.guild.id)
        cfg["enabled"] = True
        cfg["channel_id"] = channel.id
        self._save()

        await interaction.response.send_message(
            f"기본 로그 채널을 {channel.mention} 으로 설정했습니다.\n"
            "항목별 전용 채널은 `/log channel_set`으로 따로 지정할 수 있습니다.",
            ephemeral=True,
        )

        embed = self._base_embed("로그 설정 완료", discord.Color.green())
        embed.description = (
            f"앞으로 기본 로그는 이 채널에 기록합니다.\n"
            f"설정자: {interaction.user.mention}"
        )
        await self._send_log(interaction.guild, "message_edit", embed)

    @log.command(name="channel_set", description="특정 로그 항목을 보낼 채널을 따로 설정합니다.")
    @app_commands.describe(
        category="따로 분리할 로그 항목",
        channel="해당 로그 항목을 보낼 채널",
    )
    @app_commands.choices(category=CATEGORY_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def log_channel_set(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
        channel: discord.TextChannel,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return

        cfg = self._guild_config(interaction.guild.id)
        cfg["enabled"] = True
        cfg.setdefault("category_channels", {})[category.value] = channel.id
        self._save()

        await interaction.response.send_message(
            f"`{category.name}` 로그를 {channel.mention} 에 기록하도록 설정했습니다.",
            ephemeral=True,
        )

        embed = self._base_embed("로그 항목별 채널 설정", discord.Color.green())
        embed.description = f"`{category.name}` → {channel.mention}\n설정자: {interaction.user.mention}"
        await self._send_log(interaction.guild, category.value, embed)

    @log.command(name="channel_clear", description="특정 로그 항목의 전용 채널 설정을 해제합니다.")
    @app_commands.describe(category="전용 채널 설정을 해제할 로그 항목")
    @app_commands.choices(category=CATEGORY_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def log_channel_clear(self, interaction: discord.Interaction, category: app_commands.Choice[str]):
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return

        cfg = self._guild_config(interaction.guild.id)
        cfg.setdefault("category_channels", {}).pop(category.value, None)
        self._save()

        await interaction.response.send_message(
            f"`{category.name}` 전용 채널 설정을 해제했습니다. 이제 기본 로그 채널을 사용합니다.",
            ephemeral=True,
        )

    @log.command(name="show", description="현재 로그 설정을 확인합니다.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def log_show(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return

        guild = interaction.guild
        cfg = self._guild_config(guild.id)
        default_channel_id = _id(cfg.get("channel_id"))
        default_channel = guild.get_channel(default_channel_id) if default_channel_id else None
        flags = cfg.get("flags", {})

        lines = [
            f"활성화: {'ON' if cfg.get('enabled') else 'OFF'}",
            f"기본 채널: {default_channel.mention if default_channel else '설정 안 됨'}",
            "",
            "항목별 설정:",
        ]

        for key, label in LOG_CATEGORIES.items():
            enabled = "ON" if flags.get(key, True) else "OFF"
            channel_text = self._category_channel_mention(guild, key)
            lines.append(f"- {label}: {enabled} / {channel_text}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @log.command(name="disable", description="로그 기능을 전부 끕니다.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def log_disable(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return

        cfg = self._guild_config(interaction.guild.id)
        cfg["enabled"] = False
        self._save()

        await interaction.response.send_message("로그 기능을 껐습니다.", ephemeral=True)

    @log.command(name="toggle", description="특정 로그 항목을 켜거나 끕니다.")
    @app_commands.describe(
        category="변경할 로그 항목",
        enabled="켜려면 True, 끄려면 False",
    )
    @app_commands.choices(category=CATEGORY_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def log_toggle(self, interaction: discord.Interaction, category: app_commands.Choice[str], enabled: bool):
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return

        cfg = self._guild_config(interaction.guild.id)
        cfg.setdefault("flags", DEFAULT_FLAGS.copy())
        cfg["flags"][category.value] = enabled
        self._save()

        await interaction.response.send_message(
            f"`{category.name}` 로그를 {'켰습니다' if enabled else '껐습니다'}.",
            ephemeral=True,
        )

    # ---------- message logs ----------
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if not self._flag_enabled(message.guild.id, "message_delete"):
            return
        if message.channel.id in self._all_log_channel_ids(message.guild.id):
            return

        embed = self._base_embed("메시지 삭제", discord.Color.red())
        embed.add_field(name="작성자", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
        embed.add_field(name="채널", value=message.channel.mention, inline=True)
        embed.add_field(name="내용", value=_cut(message.content, 1000), inline=False)

        if message.attachments:
            lines = [f"[{a.filename}]({a.url})" for a in message.attachments[:5]]
            if len(message.attachments) > 5:
                lines.append(f"외 {len(message.attachments) - 5}개")
            embed.add_field(name="첨부파일", value=_cut("\n".join(lines), 1000), inline=False)

        await self._send_log(message.guild, "message_delete", embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None or before.author.bot:
            return
        if not self._flag_enabled(before.guild.id, "message_edit"):
            return
        if before.content == after.content:
            return
        if before.channel.id in self._all_log_channel_ids(before.guild.id):
            return

        embed = self._base_embed("메시지 수정", discord.Color.orange())
        embed.add_field(name="작성자", value=f"{before.author.mention} (`{before.author.id}`)", inline=False)
        embed.add_field(name="채널", value=before.channel.mention, inline=True)
        embed.add_field(name="이전 내용", value=_cut(before.content, 900), inline=False)
        embed.add_field(name="수정 후", value=_cut(after.content, 900), inline=False)
        embed.add_field(name="메시지 링크", value=f"[바로가기]({after.jump_url})", inline=False)

        await self._send_log(before.guild, "message_edit", embed)

    # ---------- member logs ----------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not self._flag_enabled(member.guild.id, "member_join_leave"):
            return

        embed = self._base_embed("멤버 입장", discord.Color.green())
        embed.add_field(name="유저", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="계정 생성일", value=discord.utils.format_dt(member.created_at, style="F"), inline=False)
        await self._send_log(member.guild, "member_join_leave", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not self._flag_enabled(member.guild.id, "member_join_leave"):
            return

        embed = self._base_embed("멤버 퇴장", discord.Color.dark_grey())
        embed.add_field(name="유저", value=f"{member} (`{member.id}`)", inline=False)
        if member.joined_at:
            embed.add_field(name="서버 입장일", value=discord.utils.format_dt(member.joined_at, style="F"), inline=False)
        await self._send_log(member.guild, "member_join_leave", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if not self._flag_enabled(after.guild.id, "member_update"):
            return

        if before.nick != after.nick:
            embed = self._base_embed("닉네임 변경", discord.Color.blurple())
            embed.add_field(name="유저", value=f"{after.mention} (`{after.id}`)", inline=False)
            embed.add_field(name="이전", value=before.nick or before.name, inline=True)
            embed.add_field(name="이후", value=after.nick or after.name, inline=True)
            await self._send_log(after.guild, "member_update", embed)

        before_roles = set(before.roles)
        after_roles = set(after.roles)
        added = [r for r in after_roles - before_roles if r.name != "@everyone"]
        removed = [r for r in before_roles - after_roles if r.name != "@everyone"]
        if added or removed:
            embed = self._base_embed("역할 변경", discord.Color.blurple())
            embed.add_field(name="유저", value=f"{after.mention} (`{after.id}`)", inline=False)
            if added:
                embed.add_field(name="추가", value=_cut(", ".join(role.mention for role in added), 1000), inline=False)
            if removed:
                embed.add_field(name="제거", value=_cut(", ".join(role.mention for role in removed), 1000), inline=False)
            await self._send_log(after.guild, "member_update", embed)

    # ---------- channel logs ----------
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if not self._flag_enabled(channel.guild.id, "channel_update"):
            return
        if channel.id in self._all_log_channel_ids(channel.guild.id):
            return

        embed = self._base_embed("채널 생성", discord.Color.green())
        embed.add_field(name="채널", value=f"{_name(channel)} (`{channel.id}`)", inline=False)
        embed.add_field(name="종류", value=type(channel).__name__, inline=True)
        await self._send_log(channel.guild, "channel_update", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if not self._flag_enabled(channel.guild.id, "channel_update"):
            return

        embed = self._base_embed("채널 삭제", discord.Color.red())
        embed.add_field(name="채널", value=f"{channel.name} (`{channel.id}`)", inline=False)
        embed.add_field(name="종류", value=type(channel).__name__, inline=True)
        await self._send_log(channel.guild, "channel_update", embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        if not self._flag_enabled(after.guild.id, "channel_update"):
            return
        if before.name == after.name:
            return
        if after.id in self._all_log_channel_ids(after.guild.id):
            return

        embed = self._base_embed("채널 이름 변경", discord.Color.orange())
        embed.add_field(name="이전", value=f"{before.name} (`{before.id}`)", inline=False)
        embed.add_field(name="이후", value=f"{after.name} (`{after.id}`)", inline=False)
        await self._send_log(after.guild, "channel_update", embed)

    # ---------- voice logs ----------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        if not self._flag_enabled(member.guild.id, "voice_update"):
            return

        if before.channel == after.channel:
            return

        if before.channel is None and after.channel is not None:
            title = "음성방 입장"
            color = discord.Color.green()
            desc = f"{member.mention}님이 {after.channel.mention}에 입장했습니다."
        elif before.channel is not None and after.channel is None:
            title = "음성방 퇴장"
            color = discord.Color.dark_grey()
            desc = f"{member.mention}님이 {before.channel.mention}에서 나갔습니다."
        else:
            title = "음성방 이동"
            color = discord.Color.orange()
            desc = f"{member.mention}님이 {before.channel.mention} → {after.channel.mention}으로 이동했습니다."

        embed = self._base_embed(title, color)
        embed.description = desc
        embed.add_field(name="유저", value=f"{member} (`{member.id}`)", inline=False)
        await self._send_log(member.guild, "voice_update", embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatLogCog(bot))
