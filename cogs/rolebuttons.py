from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import DATA_DIR, load_json, save_json

ROLEBTN_STORAGE = DATA_DIR / "rolebuttons_config.json"

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
                return await interaction.response.send_message(f"✅ **{role.name}** 역할을 제거했습니다.", ephemeral=True)

            if has_role:
                return await interaction.response.send_message(f"이미 **{role.name}** 역할을 가지고 있습니다.", ephemeral=True)

            await member.add_roles(role, reason="Role button assign")
            return await interaction.response.send_message(f"✅ **{role.name}** 역할을 부여했습니다.", ephemeral=True)
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


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleButtons(bot))
