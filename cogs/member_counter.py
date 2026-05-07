from __future__ import annotations

import asyncio
import unicodedata
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.storage import DATA_DIR, load_json, save_json


MEMBER_COUNTER_CFG = DATA_DIR / "member_counter_config.json"

DEFAULT_CATEGORY_NAME = "✦₊˚ ⌗ ୨♡୧ 20260515 ₊˚✦"
DEFAULT_TOTAL_TEMPLATE = " ˚₊· ૮₍♡>𖥦< ₎ა All Members :: {count} ˚₊·"
DEFAULT_HUMAN_TEMPLATE = "˚₊· ૮₍♡>𖥦< ₎ა Members :: {count}+ ˚₊·"
DEFAULT_BOT_TEMPLATE = "˚₊· ૮₍♡>𖥦< ₎ა Bots :: {count}"

DISCORD_NAME_LIMIT = 100

COUNTER_TARGET_CHOICES = [
    app_commands.Choice(name="카테고리", value="category"),
    app_commands.Choice(name="전체 멤버 채널", value="total"),
    app_commands.Choice(name="순인원 채널", value="human"),
    app_commands.Choice(name="봇 채널", value="bot"),
]


def _clean_discord_name(value: str, *, fallback: str, limit: int = DISCORD_NAME_LIMIT) -> str:
    """Discord 채널/카테고리 이름용 문자열 정리. 이모지는 제거하지 않습니다."""
    value = unicodedata.normalize("NFC", str(value or "")).strip()

    for bad in ("\r", "\n", "\t"):
        value = value.replace(bad, " ")

    while "  " in value:
        value = value.replace("  ", " ")

    if not value:
        value = fallback

    return value[:limit]


def _render_count_name(template: str, count: int, *, fallback: str) -> str:
    """
    {count}만 단순 치환해서 채널 이름을 만듭니다.
    str.format()을 쓰지 않으므로 이모지/중괄호 때문에 깨질 가능성이 줄어듭니다.
    """
    template = _clean_discord_name(template, fallback=fallback)

    if "{count}" not in template:
        template = f"{template} {{count}}"

    return _clean_discord_name(
        template.replace("{count}", str(int(count))),
        fallback=fallback.replace("{count}", str(int(count))),
    )


def _validate_template(template: str, *, field_name: str) -> tuple[bool, str]:
    template = _clean_discord_name(template, fallback="")

    if not template:
        return False, f"{field_name} 이름이 비어 있습니다."

    if "{count}" not in template:
        return False, f"{field_name} 이름에는 반드시 `{{count}}`가 들어가야 합니다."

    return True, template


class MemberCounter(commands.Cog):
    """서버 멤버 수를 채널 이름으로 표시합니다."""

    counter = app_commands.Group(
        name="멤버집계",
        description="서버 멤버 수 표시 채널을 설정합니다.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[str, dict[str, Any]] = load_json(MEMBER_COUNTER_CFG, {})
        self._update_locks: dict[int, asyncio.Lock] = {}

    def cog_unload(self):
        if self.periodic_update.is_running():
            self.periodic_update.cancel()

    async def cog_load(self):
        if not self.periodic_update.is_running():
            self.periodic_update.start()

    def _save(self):
        save_json(MEMBER_COUNTER_CFG, self.config)

    def _guild_config(self, guild_id: int) -> dict[str, Any]:
        cfg = self.config.setdefault(str(guild_id), {})
        cfg.setdefault("category_name", DEFAULT_CATEGORY_NAME)
        cfg.setdefault("total_template", DEFAULT_TOTAL_TEMPLATE)
        cfg.setdefault("human_template", DEFAULT_HUMAN_TEMPLATE)
        cfg.setdefault("bot_template", DEFAULT_BOT_TEMPLATE)
        return cfg

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._update_locks:
            self._update_locks[guild_id] = asyncio.Lock()
        return self._update_locks[guild_id]

    def _count_members(self, guild: discord.Guild) -> tuple[int, int, int]:
        total = guild.member_count or len(guild.members)
        bots = sum(1 for member in guild.members if member.bot)
        humans = max(0, total - bots)
        return total, humans, bots

    def _counter_names(self, guild_id: int, total: int, humans: int, bots: int) -> tuple[str, str, str, str]:
        cfg = self._guild_config(guild_id)

        category_name = _clean_discord_name(
            cfg.get("category_name", DEFAULT_CATEGORY_NAME),
            fallback=DEFAULT_CATEGORY_NAME,
        )

        total_name = _render_count_name(
            cfg.get("total_template", DEFAULT_TOTAL_TEMPLATE),
            total,
            fallback=DEFAULT_TOTAL_TEMPLATE,
        )

        human_name = _render_count_name(
            cfg.get("human_template", DEFAULT_HUMAN_TEMPLATE),
            humans,
            fallback=DEFAULT_HUMAN_TEMPLATE,
        )

        bot_name = _render_count_name(
            cfg.get("bot_template", DEFAULT_BOT_TEMPLATE),
            bots,
            fallback=DEFAULT_BOT_TEMPLATE,
        )

        return category_name, total_name, human_name, bot_name

    async def _safe_edit_channel_name(
        self,
        channel: discord.abc.GuildChannel | None,
        new_name: str,
    ):
        if channel is None:
            return

        new_name = _clean_discord_name(new_name, fallback=channel.name)

        if channel.name == new_name:
            return

        try:
            await channel.edit(name=new_name, reason="Member counter update")
        except discord.Forbidden:
            print(f"[member_counter] 채널 이름 변경 권한 없음: {channel.id}")
        except discord.HTTPException as exc:
            print(f"[member_counter] 채널 이름 변경 실패: {type(exc).__name__}: {exc}")

    async def _safe_edit_category_name(
        self,
        category: discord.CategoryChannel | None,
        new_name: str,
    ):
        if category is None:
            return

        new_name = _clean_discord_name(new_name, fallback=category.name)

        if category.name == new_name:
            return

        try:
            await category.edit(name=new_name, reason="Member counter category update")
        except discord.Forbidden:
            print(f"[member_counter] 카테고리 이름 변경 권한 없음: {category.id}")
        except discord.HTTPException as exc:
            print(f"[member_counter] 카테고리 이름 변경 실패: {type(exc).__name__}: {exc}")

    async def _update_guild_counter(self, guild: discord.Guild):
        cfg = self._guild_config(guild.id)

        category_id = cfg.get("category_id")
        total_channel_id = cfg.get("total_channel_id")
        human_channel_id = cfg.get("human_channel_id")
        bot_channel_id = cfg.get("bot_channel_id")

        if not total_channel_id or not human_channel_id or not bot_channel_id:
            return

        async with self._lock_for(guild.id):
            total, humans, bots = self._count_members(guild)
            category_name, total_name, human_name, bot_name = self._counter_names(guild.id, total, humans, bots)

            category = guild.get_channel(int(category_id)) if category_id else None
            total_channel = guild.get_channel(int(total_channel_id))
            human_channel = guild.get_channel(int(human_channel_id))
            bot_channel = guild.get_channel(int(bot_channel_id))

            if isinstance(category, discord.CategoryChannel):
                await self._safe_edit_category_name(category, category_name)

            await self._safe_edit_channel_name(total_channel, total_name)
            await self._safe_edit_channel_name(human_channel, human_name)
            await self._safe_edit_channel_name(bot_channel, bot_name)

    async def _create_counter_channel(
        self,
        guild: discord.Guild,
        category: discord.CategoryChannel,
        name: str,
    ) -> discord.VoiceChannel:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                connect=False,
                speak=False,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                manage_channels=True,
            ),
        }

        return await guild.create_voice_channel(
            name=_clean_discord_name(name, fallback="멤버 집계"),
            category=category,
            overwrites=overwrites,
            reason="Member counter setup",
        )

    def _apply_name_options(
        self,
        cfg: dict[str, Any],
        *,
        category_name: Optional[str] = None,
        total_name: Optional[str] = None,
        human_name: Optional[str] = None,
        bot_name: Optional[str] = None,
    ) -> tuple[bool, str | None]:
        if category_name is not None:
            cfg["category_name"] = _clean_discord_name(category_name, fallback=DEFAULT_CATEGORY_NAME)

        checks = [
            ("total_template", total_name, "전체 멤버 채널"),
            ("human_template", human_name, "순인원 채널"),
            ("bot_template", bot_name, "봇 채널"),
        ]

        for key, value, label in checks:
            if value is None:
                continue

            ok, result = _validate_template(value, field_name=label)
            if not ok:
                return False, result

            cfg[key] = result

        return True, None

    @counter.command(name="설치", description="멤버 집계 채널을 생성하고 자동 갱신을 시작합니다.")
    @app_commands.describe(
        category_name="카테고리 이름. 이모지 사용 가능",
        total_name="전체 멤버 채널 이름. {count} 필수",
        human_name="순인원 채널 이름. {count} 필수",
        bot_name="봇 채널 이름. {count} 필수",
    )
    @app_commands.rename(
        category_name="카테고리이름",
        total_name="전체이름",
        human_name="순인원이름",
        bot_name="봇이름",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def setup_counter(
        self,
        interaction: discord.Interaction,
        category_name: Optional[str] = None,
        total_name: Optional[str] = None,
        human_name: Optional[str] = None,
        bot_name: Optional[str] = None,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.",
                ephemeral=True,
            )

        guild = interaction.guild

        await interaction.response.defer(ephemeral=True)

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            return await interaction.followup.send(
                "봇에게 `채널 관리` 권한이 필요합니다.",
                ephemeral=True,
            )

        cfg = self._guild_config(guild.id)

        ok, error = self._apply_name_options(
            cfg,
            category_name=category_name,
            total_name=total_name,
            human_name=human_name,
            bot_name=bot_name,
        )
        if not ok:
            return await interaction.followup.send(error, ephemeral=True)

        total, humans, bots = self._count_members(guild)
        category_display_name, total_display_name, human_display_name, bot_display_name = self._counter_names(
            guild.id,
            total,
            humans,
            bots,
        )

        category_id = cfg.get("category_id")
        category = guild.get_channel(int(category_id)) if category_id else None

        if not isinstance(category, discord.CategoryChannel):
            category = await guild.create_category(
                category_display_name,
                reason="Member counter setup",
            )
        else:
            await self._safe_edit_category_name(category, category_display_name)

        total_channel = guild.get_channel(int(cfg["total_channel_id"])) if cfg.get("total_channel_id") else None
        human_channel = guild.get_channel(int(cfg["human_channel_id"])) if cfg.get("human_channel_id") else None
        bot_channel = guild.get_channel(int(cfg["bot_channel_id"])) if cfg.get("bot_channel_id") else None

        if not isinstance(total_channel, discord.VoiceChannel):
            total_channel = await self._create_counter_channel(
                guild,
                category,
                total_display_name,
            )

        if not isinstance(human_channel, discord.VoiceChannel):
            human_channel = await self._create_counter_channel(
                guild,
                category,
                human_display_name,
            )

        if not isinstance(bot_channel, discord.VoiceChannel):
            bot_channel = await self._create_counter_channel(
                guild,
                category,
                bot_display_name,
            )

        cfg["category_id"] = category.id
        cfg["total_channel_id"] = total_channel.id
        cfg["human_channel_id"] = human_channel.id
        cfg["bot_channel_id"] = bot_channel.id
        self._save()

        await self._update_guild_counter(guild)

        await interaction.followup.send(
            "멤버 집계 채널을 설치했습니다.\n"
            "이제부터 멤버 입장/퇴장 시 자동으로 채널 이름이 갱신됩니다.",
            ephemeral=True,
        )

    @counter.command(name="이름설정", description="멤버 집계 카테고리/채널 이름을 변경합니다.")
    @app_commands.describe(
        target="변경할 대상",
        name="새 이름. 채널 이름은 {count} 필수, 이모지 사용 가능",
    )
    @app_commands.rename(target="대상", name="이름")
    @app_commands.choices(target=COUNTER_TARGET_CHOICES)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def set_counter_name(
        self,
        interaction: discord.Interaction,
        target: app_commands.Choice[str],
        name: str,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        cfg = self._guild_config(interaction.guild.id)
        target_value = target.value

        if target_value == "category":
            cfg["category_name"] = _clean_discord_name(name, fallback=DEFAULT_CATEGORY_NAME)
        elif target_value == "total":
            ok, result = _validate_template(name, field_name="전체 멤버 채널")
            if not ok:
                return await interaction.followup.send(result, ephemeral=True)
            cfg["total_template"] = result
        elif target_value == "human":
            ok, result = _validate_template(name, field_name="순인원 채널")
            if not ok:
                return await interaction.followup.send(result, ephemeral=True)
            cfg["human_template"] = result
        elif target_value == "bot":
            ok, result = _validate_template(name, field_name="봇 채널")
            if not ok:
                return await interaction.followup.send(result, ephemeral=True)
            cfg["bot_template"] = result
        else:
            return await interaction.followup.send("알 수 없는 대상입니다.", ephemeral=True)

        self._save()
        await self._update_guild_counter(interaction.guild)

        await interaction.followup.send(
            f"`{target.name}` 이름 설정을 변경했습니다.",
            ephemeral=True,
        )

    @counter.command(name="보기", description="현재 멤버 집계 설정을 확인합니다.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def show_counter(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.",
                ephemeral=True,
            )

        cfg = self._guild_config(interaction.guild.id)

        category = interaction.guild.get_channel(int(cfg["category_id"])) if cfg.get("category_id") else None
        total_channel = interaction.guild.get_channel(int(cfg["total_channel_id"])) if cfg.get("total_channel_id") else None
        human_channel = interaction.guild.get_channel(int(cfg["human_channel_id"])) if cfg.get("human_channel_id") else None
        bot_channel = interaction.guild.get_channel(int(cfg["bot_channel_id"])) if cfg.get("bot_channel_id") else None

        embed = discord.Embed(
            title="멤버 집계 설정",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="카테고리 이름",
            value=cfg.get("category_name", DEFAULT_CATEGORY_NAME),
            inline=False,
        )
        embed.add_field(
            name="전체 멤버 이름",
            value=cfg.get("total_template", DEFAULT_TOTAL_TEMPLATE),
            inline=False,
        )
        embed.add_field(
            name="순인원 이름",
            value=cfg.get("human_template", DEFAULT_HUMAN_TEMPLATE),
            inline=False,
        )
        embed.add_field(
            name="봇 이름",
            value=cfg.get("bot_template", DEFAULT_BOT_TEMPLATE),
            inline=False,
        )

        embed.add_field(
            name="연결된 채널",
            value=(
                f"카테고리: {category.name if isinstance(category, discord.CategoryChannel) else '없음'}\n"
                f"전체: {total_channel.name if total_channel else '없음'}\n"
                f"순인원: {human_channel.name if human_channel else '없음'}\n"
                f"봇: {bot_channel.name if bot_channel else '없음'}"
            ),
            inline=False,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @counter.command(name="갱신", description="멤버 집계 채널 이름을 즉시 갱신합니다.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def refresh_counter(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        await self._update_guild_counter(interaction.guild)
        await interaction.followup.send("멤버 집계 채널을 갱신했습니다.", ephemeral=True)

    @counter.command(name="제거", description="멤버 집계 설정을 제거합니다. 채널은 직접 삭제해야 합니다.")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def remove_counter(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.",
                ephemeral=True,
            )

        self.config.pop(str(interaction.guild.id), None)
        self._save()

        await interaction.response.send_message(
            "멤버 집계 설정을 제거했습니다. 생성된 채널은 필요하면 직접 삭제해주세요.",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._update_guild_counter(member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self._update_guild_counter(member.guild)

    @tasks.loop(minutes=10)
    async def periodic_update(self):
        await self.bot.wait_until_ready()

        for guild in self.bot.guilds:
            try:
                await self._update_guild_counter(guild)
            except Exception as exc:
                print(f"[member_counter] periodic update failed: guild={guild.id}, {type(exc).__name__}: {exc}")

    @periodic_update.before_loop
    async def before_periodic_update(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(MemberCounter(bot))
