# cogs/invites.py
from __future__ import annotations
from typing import Dict, Optional
import asyncio

import discord
from discord.ext import commands
from discord import app_commands

from utils.storage import load_json, save_json, DATA_DIR

INVITE_CACHE = DATA_DIR / "invite_cache.json"       # {guild_id: {code: {"uses": int, "inviter_id": int}}}
INVITE_CFG   = DATA_DIR / "invite_log_config.json"  # {guild_id: {"log_channel_id": int}}

def _load_cache() -> Dict[str, Dict]:
    return load_json(INVITE_CACHE, {})

def _save_cache(d: Dict[str, Dict]):
    save_json(INVITE_CACHE, d)

def _load_cfg() -> Dict[str, Dict]:
    return load_json(INVITE_CFG, {})

def _save_cfg(d: Dict[str, Dict]):
    save_json(INVITE_CFG, d)

async def _snapshot_guild_invites(guild: discord.Guild) -> Dict[str, Dict]:
    """
    길드의 초대 목록을 스냅샷(코드별 사용수/초대한 유저)으로 가져옵니다.
    Manage Guild 권한이 없으면 빈 dict.
    """
    snap: Dict[str, Dict] = {}
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        return snap
    except discord.HTTPException:
        return snap

    for inv in invites:
        code = inv.code
        uses = int(getattr(inv, "uses", 0) or 0)
        inviter_id = inv.inviter.id if inv.inviter else None
        snap[code] = {"uses": uses, "inviter_id": inviter_id}
    # Vanity URL (있을 수 있음)
    try:
        vanity = await guild.vanity_invite()
        if vanity:
            uses = int(getattr(vanity, "uses", 0) or 0)
            snap[f"vanity:{vanity.code}"] = {"uses": uses, "inviter_id": None}
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass
    return snap

class Invites(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------- 설정 커맨드 -------------
    invitelog = app_commands.Group(name="invitelog", description="초대자 추적 로그 설정")

    @invitelog.command(name="set", description="초대자 로그를 보낼 텍스트 채널을 지정합니다.")
    @app_commands.describe(channel="초대자 로그를 보낼 채널")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def invitelog_set(self, interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = _load_cfg()
        cfg[str(interaction.guild.id)] = {"log_channel_id": channel.id}
        _save_cfg(cfg)
        await interaction.response.send_message(f"✅ 초대자 로그 채널을 {channel.mention} 로 설정했습니다.", ephemeral=True)

    @invitelog.command(name="show", description="현재 초대자 로그 설정을 보여줍니다.")
    async def invitelog_show(self, interaction: discord.Interaction):
        cfg = _load_cfg().get(str(interaction.guild.id), {})
        ch = interaction.guild.get_channel(cfg.get("log_channel_id", 0)) if cfg else None
        await interaction.response.send_message(
            f"현재 로그 채널: {ch.mention if ch else '미지정'}\n"
            "봇에 **Manage Guild(서버 관리)** 권한이 있어야 초대 추적이 정확합니다.",
            ephemeral=True
        )

    # ------------- 캐시 빌드 -------------

    async def _build_cache_for_guild(self, guild: discord.Guild):
        cache = _load_cache()
        snap = await _snapshot_guild_invites(guild)
        cache[str(guild.id)] = snap
        _save_cache(cache)

    @commands.Cog.listener()
    async def on_ready(self):
        # 봇 준비 시, 가입된 길드들의 초대 상태를 스냅샷
        await asyncio.gather(*(self._build_cache_for_guild(g) for g in self.bot.guilds))

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._build_cache_for_guild(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        # 새 초대 생성 시 캐시 갱신
        try:
            cache = _load_cache()
            gdict = cache.get(str(invite.guild.id), {})
            gdict[str(invite.code)] = {
                "uses": int(getattr(invite, "uses", 0) or 0),
                "inviter_id": invite.inviter.id if invite.inviter else None
            }
            cache[str(invite.guild.id)] = gdict
            _save_cache(cache)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        try:
            cache = _load_cache()
            gdict = cache.get(str(invite.guild.id), {})
            gdict.pop(str(invite.code), None)
            cache[str(invite.guild.id)] = gdict
            _save_cache(cache)
        except Exception:
            pass

    # ------------- 핵심: 누가 초대했나 -------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        cfg = _load_cfg().get(str(guild.id), {})
        log_ch_id = cfg.get("log_channel_id")
        log_ch: Optional[discord.TextChannel] = guild.get_channel(log_ch_id) if log_ch_id else None
        if log_ch is None:
            # 설정 안 되어 있으면 조용히 패스
            return

        # 이전 스냅샷과 비교
        cache = _load_cache()
        before = cache.get(str(guild.id), {})
        after = await _snapshot_guild_invites(guild)
        cache[str(guild.id)] = after  # 캐시는 최신으로 갱신
        _save_cache(cache)

        used_code = None
        inviter_id = None

        # 1) 일반 초대 코드 차이 탐지
        for code, info in after.items():
            if code.startswith("vanity:"):
                continue
            prev_uses = int(before.get(code, {}).get("uses", 0))
            now_uses  = int(info.get("uses", 0))
            if now_uses > prev_uses:
                used_code = code
                inviter_id = info.get("inviter_id")
                break

        # 2) 아니면 vanity 사용 증가 탐지
        if used_code is None:
            vanity_after = {k: v for k, v in after.items() if k.startswith("vanity:")}
            vanity_before = {k: v for k, v in before.items() if k.startswith("vanity:")}
            for code, info in vanity_after.items():
                prev = int(vanity_before.get(code, {}).get("uses", 0))
                now = int(info.get("uses", 0))
                if now > prev:
                    used_code = code
                    inviter_id = None
                    break

        # 메시지 조립
        if used_code is None:
            txt = f"👤 {member.mention} 님이 입장했습니다. 초대자: **알 수 없음**"
        else:
            if used_code.startswith("vanity:"):
                vanity_code = used_code.split(":", 1)[1]
                txt = f"👤 {member.mention} 님이 입장했습니다. 초대 링크: **Vanity URL `{vanity_code}`**"
            else:
                inv_user = guild.get_member(inviter_id) if inviter_id else None
                inv_txt = inv_user.mention if inv_user else (f"`{inviter_id}`" if inviter_id else "알 수 없음")
                txt = f"👤 {member.mention} 님이 입장했습니다. 초대자: {inv_txt} (코드: `{used_code}`)"

        try:
            await log_ch.send(txt, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.Forbidden:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(Invites(bot))
