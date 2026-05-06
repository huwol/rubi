from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import BASE_DIR, DATA_DIR, load_json, save_json

VOICE_CFG = DATA_DIR / "voice_stats_config.json"
DB_PATH = Path(BASE_DIR / "voice_stats.db")


def _fmt_seconds(total_seconds: int) -> str:
    hours, rem = divmod(int(total_seconds), 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}시간 {minutes}분 {seconds}초"
    if minutes:
        return f"{minutes}분 {seconds}초"
    return f"{seconds}초"


class VoiceStats(commands.Cog):
    """특정 역할 유저의 음성 채널 체류 시간을 SQLite에 누적 저장합니다."""

    config = app_commands.Group(name="voicestats_config", description="음성 통계 추적 설정")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_sessions: Dict[Tuple[int, int], Dict] = {}
        self._init_db()

    def _cfg_all(self) -> Dict[str, Dict]:
        return load_json(VOICE_CFG, {})

    def _save_cfg_all(self, cfg: Dict[str, Dict]) -> None:
        save_json(VOICE_CFG, cfg)

    def _tracked_role_ids(self, guild_id: int) -> List[int]:
        cfg = self._cfg_all().get(str(guild_id), {})
        return [int(x) for x in cfg.get("tracked_role_ids", []) if str(x).isdigit()]

    def _init_db(self) -> None:
        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_stats (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    total_seconds INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id, channel_id)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _add_session_time(self, guild_id: int, user_id: int, channel_id: int, seconds: int) -> None:
        if seconds <= 0:
            return
        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute(
                """
                INSERT INTO voice_stats (guild_id, user_id, channel_id, total_seconds)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, channel_id)
                DO UPDATE SET total_seconds = total_seconds + excluded.total_seconds
                """,
                (guild_id, user_id, channel_id, seconds),
            )
            conn.commit()
        finally:
            conn.close()

    def _is_tracked_member(self, member: discord.Member) -> bool:
        if member.bot:
            return False
        tracked = set(self._tracked_role_ids(member.guild.id))
        if not tracked:
            return False
        return any(role.id in tracked for role in member.roles)

    async def cog_load(self):
        self.bot.loop.create_task(self._seed_active_sessions_after_ready())

    async def _seed_active_sessions_after_ready(self):
        await self.bot.wait_until_ready()
        now = datetime.now(timezone.utc)
        seeded = 0
        for guild in self.bot.guilds:
            if not self._tracked_role_ids(guild.id):
                continue
            for channel in guild.voice_channels:
                for member in channel.members:
                    if self._is_tracked_member(member):
                        self.active_sessions[(guild.id, member.id)] = {
                            "channel_id": channel.id,
                            "joined_at": now,
                        }
                        seeded += 1
        if seeded:
            print(f"[voice_stats] active sessions seeded: {seeded}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        key = (member.guild.id, member.id)
        now = datetime.now(timezone.utc)

        # 추적 역할을 잃었거나 추적 대상이 아니면 기존 세션만 마감하고 종료합니다.
        if not self._is_tracked_member(member):
            session = self.active_sessions.pop(key, None)
            if session:
                delta = int((now - session["joined_at"]).total_seconds())
                self._add_session_time(member.guild.id, member.id, int(session["channel_id"]), delta)
            return

        before_channel = before.channel
        after_channel = after.channel

        if before_channel is not None and after_channel is None:
            session = self.active_sessions.pop(key, None)
            if session:
                delta = int((now - session["joined_at"]).total_seconds())
                self._add_session_time(member.guild.id, member.id, int(session["channel_id"]), delta)
            return

        if before_channel is None and after_channel is not None:
            self.active_sessions[key] = {"channel_id": after_channel.id, "joined_at": now}
            return

        if before_channel is not None and after_channel is not None and before_channel.id != after_channel.id:
            session = self.active_sessions.get(key)
            if session:
                delta = int((now - session["joined_at"]).total_seconds())
                self._add_session_time(member.guild.id, member.id, int(session["channel_id"]), delta)
            self.active_sessions[key] = {"channel_id": after_channel.id, "joined_at": now}

    @config.command(name="add_role", description="음성 시간 추적 대상 역할을 추가합니다.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_add_role(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        cfg = self._cfg_all()
        rec = cfg.setdefault(str(interaction.guild.id), {})
        roles = {int(x) for x in rec.get("tracked_role_ids", [])}
        roles.add(role.id)
        rec["tracked_role_ids"] = sorted(roles)
        self._save_cfg_all(cfg)
        await interaction.response.send_message(f"✅ {role.mention} 역할을 추적 대상에 추가했습니다.", ephemeral=True)

    @config.command(name="remove_role", description="음성 시간 추적 대상 역할을 제거합니다.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_remove_role(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        cfg = self._cfg_all()
        rec = cfg.setdefault(str(interaction.guild.id), {})
        roles = {int(x) for x in rec.get("tracked_role_ids", [])}
        roles.discard(role.id)
        rec["tracked_role_ids"] = sorted(roles)
        self._save_cfg_all(cfg)
        await interaction.response.send_message(f"✅ {role.mention} 역할을 추적 대상에서 제거했습니다.", ephemeral=True)

    @config.command(name="show", description="음성 통계 추적 설정을 보여줍니다.")
    async def config_show(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        role_ids = self._tracked_role_ids(interaction.guild.id)
        if not role_ids:
            return await interaction.response.send_message("추적 대상 역할이 없습니다. `/voicestats_config add_role`을 사용하세요.", ephemeral=True)
        lines = []
        for rid in role_ids:
            role = interaction.guild.get_role(rid)
            lines.append(role.mention if role else f"삭제된 역할 `{rid}`")
        await interaction.response.send_message("추적 대상 역할:\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="voicestats_user", description="특정 유저의 음성 채널 체류 시간을 보여줍니다.")
    @app_commands.describe(user="통계를 조회할 유저")
    async def voicestats_user(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return await interaction.followup.send("서버에서만 사용할 수 있습니다.", ephemeral=True)

        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute(
                """
                SELECT channel_id, total_seconds
                FROM voice_stats
                WHERE guild_id = ? AND user_id = ?
                ORDER BY total_seconds DESC
                """,
                (guild.id, user.id),
            )
            rows = c.fetchall()
        finally:
            conn.close()

        key = (guild.id, user.id)
        now = datetime.now(timezone.utc)
        if key in self.active_sessions:
            session = self.active_sessions[key]
            extra = int((now - session["joined_at"]).total_seconds())
            chan_map = {int(r[0]): int(r[1]) for r in rows}
            chan_map[int(session["channel_id"])] = chan_map.get(int(session["channel_id"]), 0) + extra
            rows = sorted(chan_map.items(), key=lambda x: x[1], reverse=True)

        if not rows:
            return await interaction.followup.send(f"{user.mention} 님의 음성 채널 기록이 없습니다.", ephemeral=True)

        embed = discord.Embed(title=f"{user.display_name} 님의 음성 채널 이용 통계", color=discord.Color.blue())
        for channel_id, total_seconds in rows[:10]:
            channel = guild.get_channel(int(channel_id))
            name = channel.name if channel else f"삭제된 채널 ({channel_id})"
            embed.add_field(name=name, value=_fmt_seconds(int(total_seconds)), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="voicestats_role", description="특정 역할 유저들의 음성 채널 체류 시간을 합산해서 보여줍니다.")
    @app_commands.describe(role="통계를 조회할 역할")
    async def voicestats_role(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return await interaction.followup.send("서버에서만 사용할 수 있습니다.", ephemeral=True)

        members = [m for m in guild.members if role in m.roles and not m.bot]
        if not members:
            return await interaction.followup.send(f"{role.mention} 역할을 가진 유저가 없습니다.", ephemeral=True)

        member_ids = [m.id for m in members]
        placeholders = ",".join("?" for _ in member_ids)
        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute(
                f"""
                SELECT channel_id, SUM(total_seconds)
                FROM voice_stats
                WHERE guild_id = ? AND user_id IN ({placeholders})
                GROUP BY channel_id
                ORDER BY SUM(total_seconds) DESC
                """,
                [guild.id] + member_ids,
            )
            rows = c.fetchall()
        finally:
            conn.close()

        now = datetime.now(timezone.utc)
        chan_map = {int(r[0]): int(r[1] or 0) for r in rows}
        for member in members:
            session = self.active_sessions.get((guild.id, member.id))
            if session:
                extra = int((now - session["joined_at"]).total_seconds())
                cid = int(session["channel_id"])
                chan_map[cid] = chan_map.get(cid, 0) + extra

        if not chan_map:
            return await interaction.followup.send(f"{role.mention} 역할 유저들의 음성 채널 기록이 없습니다.", ephemeral=True)

        embed = discord.Embed(title=f"{role.name} 역할 유저들의 음성 채널 이용 통계", color=discord.Color.green())
        for channel_id, total_seconds in sorted(chan_map.items(), key=lambda x: x[1], reverse=True)[:10]:
            channel = guild.get_channel(int(channel_id))
            name = channel.name if channel else f"삭제된 채널 ({channel_id})"
            embed.add_field(name=name, value=_fmt_seconds(int(total_seconds)), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceStats(bot))
