from __future__ import annotations

import unicodedata
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import DATA_DIR, load_json, save_json

TEMPVOICE_CFG = DATA_DIR / "tempvoice_config.json"
TEMPVOICE_STATE = DATA_DIR / "tempvoice_state.json"

CHANNEL_NAME_LIMIT = 100


def _clean_channel_name(name: str, *, fallback: str = "음성방") -> str:
    """
    Discord 음성 채널 이름용 문자열 정리.
    이모지는 제거하지 않고 NFC 정규화만 적용합니다.
    """
    value = unicodedata.normalize("NFC", str(name or "")).strip()

    for bad in ("\r", "\n", "\t"):
        value = value.replace(bad, " ")

    while "  " in value:
        value = value.replace("  ", " ")

    if not value:
        value = fallback

    return value[:CHANNEL_NAME_LIMIT]


def _load_cfg() -> Dict[str, Dict[str, Any]]:
    return load_json(TEMPVOICE_CFG, {})


def _save_cfg(data: Dict[str, Dict[str, Any]]) -> None:
    save_json(TEMPVOICE_CFG, data)


def _load_state() -> Dict[str, Dict]:
    return load_json(TEMPVOICE_STATE, {"created": {}, "counter": {}})


def _save_state(data: Dict[str, Dict]) -> None:
    data.setdefault("created", {})
    data.setdefault("counter", {})
    save_json(TEMPVOICE_STATE, data)


class TempVoice(commands.Cog):
    """허브 음성방 입장 시 임시 음성방 생성. 여러 허브를 지원합니다."""

    voicehub = app_commands.Group(name="voicehub", description="임시 음성방 허브 설정")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cfg: Dict[str, Dict[str, Any]] = _load_cfg()
        self._migrate_config_if_needed()

        state = _load_state()
        self._created: Dict[str, Dict] = state.get("created", {})
        self._counter: Dict[str, int] = {k: int(v) for k, v in state.get("counter", {}).items()}

    async def cog_load(self):
        # 봇이 완전히 준비된 뒤 비어 있는 임시방 정리
        self.bot.loop.create_task(self._cleanup_after_ready())

    def _migrate_config_if_needed(self) -> None:
        """
        기존 단일 허브 설정:
        {
            "guild_id": {
                "hub_id": ...,
                "category_id": ...,
                "naming": ...,
                ...
            }
        }

        새 다중 허브 설정:
        {
            "guild_id": {
                "hubs": {
                    "hub_id": {
                        "hub_id": ...,
                        "category_id": ...,
                        "naming": ...,
                        ...
                    }
                }
            }
        }
        """
        changed = False

        for guild_id, rec in list(self._cfg.items()):
            if not isinstance(rec, dict):
                self._cfg[guild_id] = {"hubs": {}}
                changed = True
                continue

            if "hubs" in rec:
                if not isinstance(rec.get("hubs"), dict):
                    rec["hubs"] = {}
                    changed = True
                continue

            if "hub_id" in rec:
                hub_id = str(rec.get("hub_id"))
                old = dict(rec)
                old["hub_id"] = int(old["hub_id"])
                self._cfg[guild_id] = {"hubs": {hub_id: old}}
                changed = True
            else:
                rec["hubs"] = {}
                changed = True

        if changed:
            _save_cfg(self._cfg)

    def _guild_hubs(self, guild_id: int) -> Dict[str, Dict[str, Any]]:
        gid = str(guild_id)
        rec = self._cfg.setdefault(gid, {})
        hubs = rec.setdefault("hubs", {})
        if not isinstance(hubs, dict):
            hubs = {}
            rec["hubs"] = hubs
        return hubs

    def _get_hub_rec(self, guild_id: int, hub_id: int) -> Optional[Dict[str, Any]]:
        return self._guild_hubs(guild_id).get(str(hub_id))

    def _persist_cfg(self) -> None:
        _save_cfg(self._cfg)

    async def _cleanup_after_ready(self):
        await self.bot.wait_until_ready()
        changed = False

        for channel_id in list(self._created.keys()):
            ch = self.bot.get_channel(int(channel_id))

            if ch is None:
                self._created.pop(channel_id, None)
                changed = True
                continue

            if isinstance(ch, discord.VoiceChannel) and len(ch.members) == 0:
                try:
                    await ch.delete(reason="Temp voice cleanup after bot restart")
                except (discord.Forbidden, discord.HTTPException):
                    pass

                self._created.pop(channel_id, None)
                changed = True

        if changed:
            self._persist_state()

    def _persist_state(self) -> None:
        _save_state({"created": self._created, "counter": self._counter})

    def _build_temp_channel_name(
        self,
        *,
        rec: Dict[str, Any],
        hub: discord.VoiceChannel,
        member: discord.Member,
        seq: int,
    ) -> str:
        # naming을 설정하지 않으면 허브 음성채널의 현재 이름을 그대로 기본값으로 사용
        name_pat = rec.get("naming")
        if not name_pat:
            name_pat = hub.name

        name = str(name_pat)
        name = name.replace("{user}", member.display_name)
        name = name.replace("{id}", str(seq))

        return _clean_channel_name(name, fallback=hub.name or "음성방")

    @voicehub.command(name="setup", description="허브 음성방을 추가/수정합니다. 이 방에 들어가면 임시 음성방이 생성됩니다.")
    @app_commands.describe(
        hub="허브로 사용할 음성 채널. 여러 개 설정 가능",
        category="새 음성방이 생성될 카테고리. 미지정 시 허브와 동일 카테고리",
        naming='새 음성방 이름 패턴. 미지정 시 허브 채널 이름 사용. 사용 가능: {user}, {id}',
        user_limit="최대 인원. 0은 제한 없음",
        bitrate="비트레이트 kbps. 서버 한도 내에서만 적용",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def voicehub_setup(
        self,
        interaction: discord.Interaction,
        hub: discord.VoiceChannel,
        category: Optional[discord.CategoryChannel] = None,
        naming: Optional[str] = None,
        user_limit: Optional[int] = 0,
        bitrate: Optional[int] = None,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        # naming을 안 넣으면 None으로 저장해서, 생성 시점의 허브 채널 이름을 따라가게 함
        cleaned_naming = _clean_channel_name(naming, fallback="") if naming is not None else None
        if cleaned_naming == "":
            cleaned_naming = None

        rec = {
            "hub_id": hub.id,
            "category_id": category.id if category else None,
            "naming": cleaned_naming,
            "user_limit": max(0, min(99, int(user_limit or 0))),
            "bitrate": int(bitrate) if bitrate else None,
        }

        hubs = self._guild_hubs(interaction.guild.id)
        hubs[str(hub.id)] = rec
        self._persist_cfg()

        await interaction.response.send_message(
            f"✅ 허브 설정 완료\n"
            f"- 허브: {hub.mention}\n"
            f"- 카테고리: {category.mention if category else '허브와 동일'}\n"
            f"- 이름 패턴: `{rec['naming'] if rec['naming'] else '허브 채널 이름 사용'}`\n"
            f"- 인원 제한: {rec['user_limit'] or '제한 없음'}\n"
            f"- 비트레이트: {rec['bitrate'] or '서버 기본'}\n"
            f"- 현재 등록된 허브 수: {len(hubs)}개\n"
            f"- 저장 방식: JSON 저장, 재시작 후 유지",
            ephemeral=True,
        )

    @voicehub.command(name="show", description="현재 허브 설정을 보여줍니다.")
    @app_commands.describe(hub="특정 허브만 확인하려면 선택")
    async def voicehub_show(
        self,
        interaction: discord.Interaction,
        hub: Optional[discord.VoiceChannel] = None,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        hubs = self._guild_hubs(interaction.guild.id)
        if not hubs:
            return await interaction.response.send_message("허브가 아직 설정되지 않았습니다. `/voicehub setup`을 사용하세요.", ephemeral=True)

        if hub is not None:
            rec = hubs.get(str(hub.id))
            if not rec:
                return await interaction.response.send_message("해당 음성 채널은 허브로 설정되어 있지 않습니다.", ephemeral=True)

            cat = interaction.guild.get_channel(int(rec.get("category_id") or 0)) if rec.get("category_id") else None
            return await interaction.response.send_message(
                f"허브: {hub.mention}\n"
                f"카테고리: {cat.mention if cat else '허브와 동일'}\n"
                f"패턴: `{rec.get('naming') or '허브 채널 이름 사용'}`\n"
                f"인원 제한: {rec.get('user_limit') or '없음'}\n"
                f"비트레이트: {rec.get('bitrate') or '기본'}\n"
                f"기록 중인 임시방: {len(self._created)}개",
                ephemeral=True,
            )

        lines = []
        for idx, rec in enumerate(hubs.values(), start=1):
            hub_channel = interaction.guild.get_channel(int(rec.get("hub_id") or 0))
            cat = interaction.guild.get_channel(int(rec.get("category_id") or 0)) if rec.get("category_id") else None

            lines.append(
                "\n".join(
                    [
                        f"**{idx}. {hub_channel.mention if hub_channel else '삭제된 허브'}**",
                        f"- 카테고리: {cat.mention if cat else '허브와 동일'}",
                        f"- 패턴: `{rec.get('naming') or '허브 채널 이름 사용'}`",
                        f"- 인원 제한: {rec.get('user_limit') or '없음'}",
                        f"- 비트레이트: {rec.get('bitrate') or '기본'}",
                    ]
                )
            )

        await interaction.response.send_message(
            "\n\n".join(lines) + f"\n\n기록 중인 임시방: {len(self._created)}개",
            ephemeral=True,
        )

    @voicehub.command(name="unset", description="허브 설정을 해제합니다. 허브를 지정하지 않으면 전체 해제합니다.")
    @app_commands.describe(hub="해제할 허브. 미지정 시 모든 허브 해제")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def voicehub_unset(
        self,
        interaction: discord.Interaction,
        hub: Optional[discord.VoiceChannel] = None,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        hubs = self._guild_hubs(interaction.guild.id)

        if hub is None:
            count = len(hubs)
            hubs.clear()
            self._persist_cfg()
            return await interaction.response.send_message(
                f"✅ 허브 설정 {count}개를 모두 해제했습니다. 기존 임시방은 자동 삭제 대상에 남아 있습니다.",
                ephemeral=True,
            )

        removed = hubs.pop(str(hub.id), None)
        self._persist_cfg()

        if removed:
            await interaction.response.send_message(
                f"✅ {hub.mention} 허브 설정을 해제했습니다. 기존 임시방은 자동 삭제 대상에 남아 있습니다.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{hub.mention} 채널은 허브로 설정되어 있지 않습니다.",
                ephemeral=True,
            )

    @voicehub.command(name="rename", description="허브별 임시방 이름 패턴을 변경합니다.")
    @app_commands.describe(
        hub="설정을 변경할 허브",
        naming="새 이름 패턴. 비워두면 허브 채널 이름 사용. 사용 가능: {user}, {id}",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def voicehub_rename(
        self,
        interaction: discord.Interaction,
        hub: discord.VoiceChannel,
        naming: Optional[str] = None,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        rec = self._get_hub_rec(interaction.guild.id, hub.id)
        if not rec:
            return await interaction.response.send_message("해당 음성 채널은 허브로 설정되어 있지 않습니다.", ephemeral=True)

        cleaned_naming = _clean_channel_name(naming, fallback="") if naming is not None else None
        if cleaned_naming == "":
            cleaned_naming = None

        rec["naming"] = cleaned_naming
        self._persist_cfg()

        await interaction.response.send_message(
            f"✅ {hub.mention} 허브의 임시방 이름 패턴을 "
            f"`{cleaned_naming or '허브 채널 이름 사용'}`으로 설정했습니다.",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return

        if after.channel is not None and isinstance(after.channel, discord.VoiceChannel):
            await self._maybe_create_and_move(member, after.channel)

        if before.channel is not None and isinstance(before.channel, discord.VoiceChannel):
            await self._maybe_cleanup(before.channel)

    async def _maybe_create_and_move(self, member: discord.Member, joined: discord.VoiceChannel):
        rec = self._get_hub_rec(member.guild.id, joined.id)
        if not rec:
            return

        gid = str(member.guild.id)
        self._counter[gid] = int(self._counter.get(gid, 0)) + 1

        name = self._build_temp_channel_name(
            rec=rec,
            hub=joined,
            member=member,
            seq=self._counter[gid],
        )

        category = member.guild.get_channel(int(rec.get("category_id") or 0)) if rec.get("category_id") else joined.category
        if not isinstance(category, discord.CategoryChannel):
            category = None

        overwrites = dict(joined.overwrites)
        owner_ow = overwrites.get(member, discord.PermissionOverwrite())
        owner_ow.view_channel = True
        owner_ow.manage_channels = True
        owner_ow.connect = True
        overwrites[member] = owner_ow

        try:
            new_ch = await member.guild.create_voice_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                reason=f"Temp voice created for {member} ({member.id})",
            )

            edits = {}

            if rec.get("user_limit", 0):
                edits["user_limit"] = int(rec["user_limit"])

            if rec.get("bitrate"):
                edits["bitrate"] = int(rec["bitrate"]) * 1000

            if edits:
                await new_ch.edit(**edits, reason="Temp voice settings")

        except discord.Forbidden:
            return
        except discord.HTTPException as exc:
            print(f"[tempvoice] create failed: {type(exc).__name__}: {exc}")
            return

        self._created[str(new_ch.id)] = {
            "guild_id": member.guild.id,
            "owner_id": member.id,
            "hub_id": joined.id,
        }
        self._persist_state()

        try:
            await member.move_to(new_ch, reason="Join-to-Create 이동")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    async def _maybe_cleanup(self, channel: discord.VoiceChannel):
        if str(channel.id) not in self._created:
            return

        if len(channel.members) > 0:
            return

        try:
            await channel.delete(reason="Temp voice cleanup: empty channel")
        except (discord.Forbidden, discord.HTTPException):
            return
        finally:
            self._created.pop(str(channel.id), None)
            self._persist_state()


async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoice(bot))
