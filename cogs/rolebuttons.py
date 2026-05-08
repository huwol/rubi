from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import DATA_DIR, load_json, save_json

ROLEBTN_STORAGE = DATA_DIR / "rolebuttons_config.json"
ROLEBTN_STORAGE = DATA_DIR / "rolebuttons_config.json"
ROLEBTN_STATS_STORAGE = DATA_DIR / "rolebuttons_stats.json"

STYLE_CHOICES = [
    app_commands.Choice(name="Primary(파랑)", value="primary"),
    app_commands.Choice(name="Secondary(회색)", value="secondary"),
    app_commands.Choice(name="Success(초록)", value="success"),
    app_commands.Choice(name="Danger(빨강)", value="danger"),
]


def to_button_style(s: Optional[str]) -> discord.ButtonStyle:
    return {
        "secondary": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
    }.get((s or "primary").lower(), discord.ButtonStyle.primary)
    
def _load_rolebtn_stats() -> Dict[str, Dict[str, Dict[str, List[int]]]]:
    data = load_json(ROLEBTN_STATS_STORAGE, {})
    return data if isinstance(data, dict) else {}


def _normalize_id_list(value) -> List[int]:
    if not isinstance(value, list):
        return []
    result: List[int] = []
    for x in value:
        try:
            result.append(int(x))
        except (TypeError, ValueError):
            pass
    return result


def update_rolebtn_stats(
    guild_id: int,
    role_id: int,
    user_id: int,
    *,
    active: bool,
    count_total: bool = True,
) -> Tuple[int, int]:
    """
    active: 현재 이 버튼으로 역할을 받은 상태인지
    total: 한 번이라도 이 버튼을 눌러본 유저 수
    """
    stats = _load_rolebtn_stats()

    guild_key = str(guild_id)
    role_key = str(role_id)

    guild_stats = stats.setdefault(guild_key, {})
    role_stats = guild_stats.setdefault(role_key, {"active": [], "total": []})

    active_ids = set(_normalize_id_list(role_stats.get("active")))
    total_ids = set(_normalize_id_list(role_stats.get("total")))

    uid = int(user_id)

    if count_total:
        total_ids.add(uid)

    if active:
        active_ids.add(uid)
    else:
        active_ids.discard(uid)

    role_stats["active"] = sorted(active_ids)
    role_stats["total"] = sorted(total_ids)

    save_json(ROLEBTN_STATS_STORAGE, stats)

    return len(active_ids), len(total_ids)


def get_rolebtn_stats(guild_id: int, role_id: int) -> Tuple[List[int], List[int]]:
    stats = _load_rolebtn_stats()
    role_stats = stats.get(str(guild_id), {}).get(str(role_id), {})

    active_ids = _normalize_id_list(role_stats.get("active"))
    total_ids = _normalize_id_list(role_stats.get("total"))

    return active_ids, total_ids

class RoleButton(discord.ui.Button):
    def __init__(self, guild_id: int, role_id: int, label: str, style: discord.ButtonStyle, toggle: bool):
        super().__init__(
            label=label[:80],
            style=style,
            custom_id=f"rolebtn:{guild_id}:{role_id}:{1 if toggle else 0}",
        )
        self.guild_id = int(guild_id)
        self.role_id = int(role_id)
        self.toggle = bool(toggle)

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            return await interaction.response.send_message("이 버튼은 다른 서버 설정입니다.", ephemeral=True)

        role = interaction.guild.get_role(self.role_id)
        if role is None:
            return await interaction.response.send_message("이 역할은 삭제되었거나 찾을 수 없습니다.", ephemeral=True)

        member = interaction.user
        if not isinstance(member, discord.Member):
            member = interaction.guild.get_member(interaction.user.id) or await interaction.guild.fetch_member(interaction.user.id)

        me = interaction.guild.me or await interaction.guild.fetch_member(interaction.client.user.id)
        if not me.guild_permissions.manage_roles:
            return await interaction.response.send_message("봇에 `역할 관리` 권한이 없습니다.", ephemeral=True)
        if role >= me.top_role:
            return await interaction.response.send_message("해당 역할이 봇의 최상위 역할보다 위에 있습니다.", ephemeral=True)

        has_role = role in member.roles
        try:
            if self.toggle and has_role:
                await member.remove_roles(role, reason="Role button toggle off")

                active_count, total_count = update_rolebtn_stats(
                    self.guild_id,
                    self.role_id,
                    member.id,
                    active=False,
                )

                return await interaction.response.send_message(
                    f"✅ **{role.name}** 역할을 제거했습니다.\n"
                    f"현재 버튼 등록 인원: **{active_count}명**\n"
                    f"누적 버튼 사용 인원: **{total_count}명**",
                    ephemeral=True,
                )

            if has_role:
                active_count, total_count = update_rolebtn_stats(
                    self.guild_id,
                    self.role_id,
                    member.id,
                    active=True,
                )

                return await interaction.response.send_message(
                    f"이미 **{role.name}** 역할을 가지고 있습니다.\n"
                    f"현재 버튼 등록 인원: **{active_count}명**\n"
                    f"누적 버튼 사용 인원: **{total_count}명**",
                    ephemeral=True,
                )

            await member.add_roles(role, reason="Role button assign")

            active_count, total_count = update_rolebtn_stats(
                self.guild_id,
                self.role_id,
                member.id,
                active=True,
            )

            return await interaction.response.send_message(
                f"✅ **{role.name}** 역할을 부여했습니다.\n"
                f"현재 버튼 등록 인원: **{active_count}명**\n"
                f"누적 버튼 사용 인원: **{total_count}명**",
                ephemeral=True,
            )
        except discord.Forbidden:
            return await interaction.response.send_message("권한 또는 역할 순서를 확인해 주세요.", ephemeral=True)
        except discord.HTTPException:
            return await interaction.response.send_message("Discord 요청 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)


class RoleButtonsView(discord.ui.View):
    def __init__(self, guild_id: int, buttons: List[Tuple[int, str]], style: discord.ButtonStyle, toggle: bool):
        super().__init__(timeout=None)
        for role_id, label in buttons:
            self.add_item(RoleButton(guild_id, role_id, label, style, toggle))


class RoleButtons(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # guild 캐시가 비어 있어도 role_id만으로 persistent view를 복구할 수 있게 변경.
        cfgs: List[Dict] = load_json(ROLEBTN_STORAGE, [])
        restored = 0
        for cfg in cfgs:
            try:
                guild_id = int(cfg.get("guild_id"))
                style = to_button_style(cfg.get("style"))
                toggle = bool(cfg.get("toggle", True))
                buttons: List[Tuple[int, str]] = []
                for item in cfg.get("roles", []):
                    role_id = int(item["id"])
                    buttons.append((role_id, item.get("label") or str(role_id)))
                if buttons:
                    self.bot.add_view(RoleButtonsView(guild_id, buttons, style, toggle))
                    restored += 1
            except Exception as exc:
                print(f"[rolebuttons] restore skipped: {type(exc).__name__}: {exc}")
        print(f"[rolebuttons] persistent views restored: {restored}")

    @app_commands.command(name="rolebuttons", description="역할 버튼을 한 메시지에 생성합니다. 최소 1개, 최대 6개.")
    @app_commands.describe(
        role1="버튼 #1 역할", label1="버튼 #1 라벨",
        role2="버튼 #2 역할", label2="버튼 #2 라벨",
        role3="버튼 #3 역할", label3="버튼 #3 라벨",
        role4="버튼 #4 역할", label4="버튼 #4 라벨",
        role5="버튼 #5 역할", label5="버튼 #5 라벨",
        role6="버튼 #6 역할", label6="버튼 #6 라벨",
        style="버튼 색상", toggle="켜짐: 부여/제거, 꺼짐: 부여만",
    )
    @app_commands.choices(style=STYLE_CHOICES)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def rolebuttons(
        self,
        interaction: discord.Interaction,
        role1: discord.Role,
        label1: Optional[str] = None,
        role2: Optional[discord.Role] = None,
        label2: Optional[str] = None,
        role3: Optional[discord.Role] = None,
        label3: Optional[str] = None,
        role4: Optional[discord.Role] = None,
        label4: Optional[str] = None,
        role5: Optional[discord.Role] = None,
        label5: Optional[str] = None,
        role6: Optional[discord.Role] = None,
        label6: Optional[str] = None,
        style: Optional[app_commands.Choice[str]] = None,
        toggle: Optional[bool] = True,
    ):
        if interaction.guild is None or interaction.channel is None:
            return await interaction.response.send_message("서버 채널에서만 사용할 수 있습니다.", ephemeral=True)

        me = interaction.guild.me or await interaction.guild.fetch_member(interaction.client.user.id)
        if not me.guild_permissions.manage_roles:
            return await interaction.response.send_message("봇에 `역할 관리` 권한이 없습니다.", ephemeral=True)

        provided = [(role1, label1), (role2, label2), (role3, label3), (role4, label4), (role5, label5), (role6, label6)]
        provided = [(r, lbl) for r, lbl in provided if r is not None]

        seen: set[int] = set()
        dedup: List[Tuple[discord.Role, str]] = []
        for role, label in provided:
            if role.id in seen:
                continue
            seen.add(role.id)
            if role >= me.top_role:
                return await interaction.response.send_message(
                    f"**{role.name}** 역할이 봇의 최상위 역할보다 위에 있습니다. 역할 순서를 조정해 주세요.",
                    ephemeral=True,
                )
            dedup.append((role, (label or role.name)[:80]))

        btn_style = to_button_style(style.value if style else None)
        view = RoleButtonsView(interaction.guild.id, [(r.id, lbl) for r, lbl in dedup], btn_style, bool(toggle))

        await interaction.response.send_message("✅ 역할 버튼 메시지를 전송했습니다.", ephemeral=True)
        await interaction.channel.send(
            content="아래 버튼으로 역할을 설정/해제할 수 있습니다:\n" + "\n".join(f"• {r.mention}" for r, _ in dedup),
            view=view,
            allowed_mentions=discord.AllowedMentions(roles=False, users=False, everyone=False),
        )

        cfgs: List[Dict] = load_json(ROLEBTN_STORAGE, [])
        cfgs.append({
            "guild_id": interaction.guild.id,
            "roles": [{"id": r.id, "label": lbl} for r, lbl in dedup],
            "style": style.value if style else "primary",
            "toggle": bool(toggle),
        })
        save_json(ROLEBTN_STORAGE, cfgs)
        
    @app_commands.command(name="rolebuttonstats", description="역할 버튼을 누른 인원 수를 확인합니다.")
    @app_commands.describe(
        role="확인할 역할입니다. 비워두면 이 서버의 모든 역할 버튼 기록을 보여줍니다.",
        show_users="켜면 현재 버튼 등록 유저 목록도 표시합니다.",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def rolebuttonstats(
        self,
        interaction: discord.Interaction,
        role: Optional[discord.Role] = None,
        show_users: Optional[bool] = False,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        stats = _load_rolebtn_stats()
        guild_stats = stats.get(str(interaction.guild.id), {})

        if role is not None:
            role_ids = [role.id]
        else:
            role_ids = []
            for key in guild_stats.keys():
                try:
                    role_ids.append(int(key))
                except ValueError:
                    pass

        if not role_ids:
            return await interaction.response.send_message(
                "아직 역할 버튼 통계가 없습니다.",
                ephemeral=True,
            )

        lines: List[str] = ["📊 **역할 버튼 통계**"]

        for role_id in role_ids:
            active_ids, total_ids = get_rolebtn_stats(interaction.guild.id, role_id)
            target_role = interaction.guild.get_role(role_id)

            role_name = target_role.mention if target_role else f"`삭제된 역할({role_id})`"

            lines.append(
                f"\n• {role_name}\n"
                f"  - 현재 버튼 등록 인원: **{len(active_ids)}명**\n"
                f"  - 누적 버튼 사용 인원: **{len(total_ids)}명**"
            )

            if show_users and active_ids:
                shown_users: List[str] = []

                for user_id in active_ids[:50]:
                    member = interaction.guild.get_member(user_id)
                    shown_users.append(member.mention if member else f"`{user_id}`")

                more = ""
                if len(active_ids) > 50:
                    more = f" 외 {len(active_ids) - 50}명"

                lines.append(f"  - 현재 등록 유저: {', '.join(shown_users)}{more}")

        message = "\n".join(lines)

        if len(message) > 1900:
            message = message[:1850] + "\n\n...표시할 내용이 많아서 일부만 표시했습니다."

        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(RoleButtons(bot))
