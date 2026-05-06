# cogs/tickets.py
from __future__ import annotations
from typing import Optional, Dict, Any, List
import io
import datetime as dt

import discord
from discord.ext import commands
from discord import app_commands

from utils.storage import load_json, save_json, DATA_DIR

# 저장 파일들
TICKET_CFG      = DATA_DIR / "ticket_config.json"    # 길드별 프리셋 모음
TICKET_STATE    = DATA_DIR / "ticket_state.json"     # 채널별 상태(소유자/클레임/오픈여부)
TICKET_COUNTER  = DATA_DIR / "ticket_counter.json"   # 길드별 시퀀스
TICKET_PANELS   = DATA_DIR / "ticket_panels.json"    # 패널 message_id -> (preset | buttons) 매핑

# ---------- 설정 헬퍼 (프리셋 지원) ----------
def _raw_cfg_all() -> Dict[str, Any]:
    return load_json(TICKET_CFG, {})

def _save_raw_cfg_all(d: Dict[str, Any]):
    save_json(TICKET_CFG, d)

def _migrate_if_needed(gid: int, d: Dict[str, Any]) -> Dict[str, Any]:
    s = d.get(str(gid)) or {}
    # 구(단일 설정) → 신(프리셋) 자동 마이그레이션
    if s and ("support_role_id" in s or "category_id" in s or "logs_channel_id" in s or "naming" in s):
        presets = {"default": {
            "support_role_id": s.get("support_role_id"),
            "category_id": s.get("category_id"),
            "logs_channel_id": s.get("logs_channel_id"),
            "naming": s.get("naming", "ticket-{id}")
        }}
        ns = {"presets": presets, "default": "default"}
        d[str(gid)] = ns
        _save_raw_cfg_all(d)
        return ns
    # 새 구조 기본값
    if "presets" not in s:
        s = {"presets": {}, "default": "default"}
        d[str(gid)] = s
        _save_raw_cfg_all(d)
    return s

def _guild_cfg(gid: int) -> Dict[str, Any]:
    allcfg = _raw_cfg_all()
    return _migrate_if_needed(gid, allcfg)

def _get_preset_cfg(gid: int, preset: str | None) -> Optional[Dict[str, Any]]:
    g = _guild_cfg(gid)
    if not g: return None
    if not preset:
        preset = g.get("default")
    return (g.get("presets") or {}).get(str(preset))

def _set_preset_cfg(gid: int, preset: str, cfg: Dict[str, Any], make_default: bool = False):
    allcfg = _raw_cfg_all()
    g = _migrate_if_needed(gid, allcfg)
    presets = g.setdefault("presets", {})
    presets[str(preset)] = cfg
    if make_default:
        g["default"] = str(preset)
    allcfg[str(gid)] = g
    _save_raw_cfg_all(allcfg)

def _list_presets(gid: int) -> List[str]:
    g = _guild_cfg(gid)
    return sorted((g.get("presets") or {}).keys())

# ---------- 상태/시퀀스 ----------
def _state_all() -> Dict[str, Any]:
    return load_json(TICKET_STATE, {})

def _save_state_all(s: Dict[str, Any]):
    save_json(TICKET_STATE, s)

def _next_id(gid: int) -> int:
    c = load_json(TICKET_COUNTER, {})
    key = str(gid)
    val = int(c.get(key, 0)) + 1
    c[key] = val
    save_json(TICKET_COUNTER, c)
    return val

# ---------- 패널 매핑 ----------
def _panels() -> Dict[str, Any]:
    return load_json(TICKET_PANELS, {})

def _save_panels(p: Dict[str, Any]):
    save_json(TICKET_PANELS, p)

def _register_panel_single(message_id: int, guild_id: int, channel_id: int, preset: str):
    p = _panels()
    p[str(message_id)] = {"guild_id": guild_id, "channel_id": channel_id, "preset": str(preset)}
    _save_panels(p)

def _register_panel_multi(message_id: int, guild_id: int, channel_id: int, buttons: Dict[str, Dict[str, str]]):
    """
    buttons 예:
    {"1": {"preset": "support"},
     "2": {"preset": "report"},
     "3": {"preset": "alliance"}}
    """
    p = _panels()
    p[str(message_id)] = {"guild_id": guild_id, "channel_id": channel_id, "buttons": buttons}
    _save_panels(p)

def _lookup_panel_preset(message_id: int) -> Optional[str]:
    """
    레거시(단일 버튼)용: message_id -> preset
    """
    rec = _panels().get(str(message_id))
    if not rec: return None
    if "preset" in rec:
        return rec["preset"]
    return None

def _lookup_panel_slot_preset(message_id: int, slot: str) -> Optional[str]:
    """
    멀티 버튼용: 특정 슬롯의 프리셋 조회
    """
    rec = _panels().get(str(message_id))
    if not rec: return None
    if "buttons" in rec:
        btns = rec["buttons"] or {}
        ent = btns.get(str(slot))
        if ent:
            return ent.get("preset")
    return None

# ---------- UI ----------
# (A) 레거시: 단일 버튼
class OpenTicketButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🎫 티켓 열기", style=discord.ButtonStyle.primary, custom_id="ticket:open")

    async def callback(self, interaction: discord.Interaction):
        await _handle_open_ticket(interaction, preset=_lookup_panel_preset(interaction.message.id))

# (B) 멀티 버튼: 슬롯 1~3
def _parse_style(name: Optional[str]) -> discord.ButtonStyle:
    table = {
        None: discord.ButtonStyle.primary,
        "primary": discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
    }
    return table.get((name or "").lower(), discord.ButtonStyle.primary)

async def _handle_open_ticket(interaction: discord.Interaction, preset: Optional[str]):
    if interaction.guild is None:
        return await interaction.response.send_message("DM에서는 사용할 수 없어요.", ephemeral=True)
    if preset is None:
        return await interaction.response.send_message("이 패널의 프리셋 매핑을 찾을 수 없어요. 패널을 다시 만들어 주세요.", ephemeral=True)

    cfg = _get_preset_cfg(interaction.guild.id, preset)
    if not cfg or not cfg.get("support_role_id"):
        return await interaction.response.send_message("이 프리셋 설정이 유효하지 않습니다. `/ticket setup`을 확인해 주세요.", ephemeral=True)

    support_role = interaction.guild.get_role(int(cfg["support_role_id"])) if cfg.get("support_role_id") else None
    category = interaction.guild.get_channel(int(cfg["category_id"])) if cfg.get("category_id") else None

    # 채널명 패턴
    seq = _next_id(interaction.guild.id)
    name_pat = cfg.get("naming", "ticket-{id}")
    ch_name = name_pat.replace("{id}", str(seq)).replace("{user}", interaction.user.name)

    # 권한
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
    }
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)

    ch = await interaction.guild.create_text_channel(
        name=ch_name,
        category=category if isinstance(category, discord.CategoryChannel) else None,
        overwrites=overwrites,
        topic=f"Preset:{preset or 'default'} | OwnerID:{interaction.user.id} | Opened:{dt.datetime.utcnow().isoformat()}",
        reason="Ticket created"
    )

    # 상태 기록
    st = _state_all()
    st[str(ch.id)] = {"owner_id": interaction.user.id, "claimed_by": None, "open": True, "preset": preset or "default"}
    _save_state_all(st)

    view = TicketControlsView()
    emb = discord.Embed(
        title="🎫 지원 티켓이 열렸습니다",
        description=(f"{interaction.user.mention} 님이 지원을 요청했습니다.\n아래 버튼으로 **닫기**를 할 수 있어요."),
        color=discord.Color.blurple()
    )
    if support_role:
        emb.add_field(name="지원 역할", value=support_role.mention, inline=True)
    if preset:
        emb.set_footer(text=f"preset={preset}")

    await ch.send(
        content=(support_role.mention if support_role else None),
        embed=emb,
        view=view,
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False)
    )

    await interaction.response.send_message(f"✅ 티켓을 생성했어요: {ch.mention}", ephemeral=True)

class MultiOpenButton(discord.ui.Button):
    """
    멀티 패널용 버튼 — custom_id: ticket:open:{slot}
    """
    def __init__(self, slot: int, label: str = "티켓 열기", style: discord.ButtonStyle = discord.ButtonStyle.primary):
        self.slot = str(slot)
        super().__init__(label=label, style=style, custom_id=f"ticket:open:{self.slot}")

    async def callback(self, interaction: discord.Interaction):
        msg = interaction.message
        if msg is None:
            return await interaction.response.send_message("패널 메시지를 찾을 수 없어요.", ephemeral=True)
        preset = _lookup_panel_slot_preset(msg.id, self.slot)
        await _handle_open_ticket(interaction, preset=preset)

class TicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CloseButton())

class ClosedTicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ReopenButton())
        self.add_item(DeleteButton())

def _is_support_or_manager(interaction: discord.Interaction, support_role_id: Optional[int]) -> bool:
    if interaction.guild is None:
        return False

    member = interaction.user
    if not isinstance(member, discord.Member):
        return False

    has_manage = member.guild_permissions.manage_channels
    if has_manage:
        return True

    if support_role_id is None:
        return False

    return any(role.id == int(support_role_id) for role in member.roles)


def _closed_channel_overwrites(
    guild: discord.Guild,
    support_role: Optional[discord.Role],
    owner: Optional[discord.Member],
) -> Dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """
    닫힌 티켓:
    - @everyone: 비공개
    - 티켓 소유자: 명시적으로 비공개
    - 지원 역할: 보기 허용, 작성은 막음
    - 봇: 관리 가능
    """
    overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }

    if owner:
        overwrites[owner] = discord.PermissionOverwrite(view_channel=False, send_messages=False)

    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            read_message_history=True,
            manage_messages=True,
        )

    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True,
        )

    return overwrites


def _open_channel_overwrites(
    guild: discord.Guild,
    support_role: Optional[discord.Role],
    owner: Optional[discord.Member],
) -> Dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """
    다시 열린 티켓:
    - @everyone: 비공개
    - 티켓 소유자: 보기/작성 허용
    - 지원 역할: 보기/작성/관리 허용
    - 봇: 관리 가능
    """
    overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }

    if owner:
        overwrites[owner] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        )

    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True,
        )

    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True,
        )

    return overwrites


class ReopenButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔓 다시 열기", style=discord.ButtonStyle.success, custom_id="ticket:reopen")

    async def callback(self, interaction: discord.Interaction):
        ch = interaction.channel
        if interaction.guild is None or not isinstance(ch, discord.TextChannel):
            return await interaction.response.send_message("채널에서만 사용 가능해요.", ephemeral=True)

        all_st = _state_all()
        st = all_st.get(str(ch.id))
        if not st:
            return await interaction.response.send_message("티켓 채널이 아니에요.", ephemeral=True)
        if st.get("open", True):
            return await interaction.response.send_message("이미 열린 티켓입니다.", ephemeral=True)

        preset = st.get("preset")
        cfg = _get_preset_cfg(interaction.guild.id, preset)
        support_role_id = int(cfg["support_role_id"]) if (cfg and cfg.get("support_role_id")) else None
        support_role = interaction.guild.get_role(support_role_id) if support_role_id else None

        if not _is_support_or_manager(interaction, support_role_id):
            return await interaction.response.send_message(
                "다시 열기 권한이 없습니다. (지원 역할 또는 채널 관리 권한 필요)",
                ephemeral=True,
            )

        owner_id = st.get("owner_id")
        owner = interaction.guild.get_member(int(owner_id)) if owner_id else None

        await interaction.response.defer(ephemeral=True)

        overwrites = _open_channel_overwrites(interaction.guild, support_role, owner)
        await ch.edit(overwrites=overwrites, reason=f"Ticket reopened by {interaction.user} ({interaction.user.id})")

        all_st[str(ch.id)]["open"] = True
        _save_state_all(all_st)

        if ch.name.startswith("closed-"):
            try:
                await ch.edit(name=ch.name.removeprefix("closed-"), reason="Ticket reopened")
            except Exception:
                pass

        await ch.send(
            f"🔓 이 티켓이 **다시 열렸습니다**. 다시 연 사람: {interaction.user.mention}",
            view=TicketControlsView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

        await interaction.followup.send("티켓을 다시 열었습니다.", ephemeral=True)


class DeleteButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🗑 삭제", style=discord.ButtonStyle.danger, custom_id="ticket:delete")

    async def callback(self, interaction: discord.Interaction):
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return await interaction.response.send_message("채널에서만 사용 가능해요.", ephemeral=True)

        all_st = _state_all()
        st = all_st.get(str(ch.id))
        if not st:
            return await interaction.response.send_message("티켓 채널이 아니에요.", ephemeral=True)
        if st.get("open", True):
            return await interaction.response.send_message("먼저 **닫기**를 눌러 티켓을 닫아주세요.", ephemeral=True)

        cfg = _get_preset_cfg(interaction.guild.id, st.get("preset"))
        support_id = int(cfg["support_role_id"]) if (cfg and cfg.get("support_role_id")) else None
        if not _is_support_or_manager(interaction, support_id):
            return await interaction.response.send_message("삭제 권한이 없습니다. (지원 역할 또는 채널 관리 권한 필요)", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        try:
            all_st.pop(str(ch.id), None)
            _save_state_all(all_st)
        except Exception:
            pass

        try:
            await ch.delete(reason=f"Ticket deleted by {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            try:
                await interaction.followup.send("채널을 삭제할 권한이 없습니다. 권한을 확인해 주세요.", ephemeral=True)
            except Exception:
                pass

class CloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔒 닫기", style=discord.ButtonStyle.danger, custom_id="ticket:close")

    async def callback(self, interaction: discord.Interaction):
        ch = interaction.channel
        if interaction.guild is None or not isinstance(ch, discord.TextChannel):
            return await interaction.response.send_message("채널에서만 사용 가능해요.", ephemeral=True)

        # 상태/프리셋 확인
        all_st = _state_all()
        st = all_st.get(str(ch.id))
        if not st:
            return await interaction.response.send_message("티켓 채널이 아니에요.", ephemeral=True)
        if not st.get("open", True):
            return await interaction.response.send_message("이미 닫힌 티켓입니다.", ephemeral=True)

        preset = st.get("preset")
        cfg = _get_preset_cfg(interaction.guild.id, preset)
        support_role_id = int(cfg["support_role_id"]) if (cfg and cfg.get("support_role_id")) else None
        support_role = interaction.guild.get_role(support_role_id) if support_role_id else None

        owner_id = st.get("owner_id")
        owner = interaction.guild.get_member(int(owner_id)) if owner_id else None

        # 티켓 주인 / 지원 역할 / 채널 관리 권한만 닫기 가능
        is_owner = owner_id is not None and interaction.user.id == int(owner_id)
        if not (is_owner or _is_support_or_manager(interaction, support_role_id)):
            return await interaction.response.send_message(
                "닫기 권한이 없습니다. (티켓 주인, 지원 역할, 채널 관리 권한 필요)",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        # 🔐 닫힘 권한 재구성: 지원 역할과 봇 외에는 안 보이게 함
        new_overwrites = _closed_channel_overwrites(interaction.guild, support_role, owner)
        await ch.edit(
            overwrites=new_overwrites,
            reason=f"Ticket closed by {interaction.user} ({interaction.user.id})",
        )

        # 상태 변경 및 채널명 갱신
        all_st[str(ch.id)]["open"] = False
        _save_state_all(all_st)

        if not ch.name.startswith("closed-"):
            try:
                await ch.edit(name=f"closed-{ch.name}", reason="Ticket closed")
            except Exception:
                pass

        await ch.send(
            f"🔒 이 티켓은 **닫혔습니다**. 지원 역할만 볼 수 있습니다.\n닫은 사람: {interaction.user.mention}",
            view=ClosedTicketControlsView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        await interaction.followup.send("티켓을 닫았습니다.", ephemeral=True)


# (A) 레거시 단일 패널 뷰
class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(OpenTicketButton())

# (B) 멀티 패널용 "루트" 퍼시스턴트 뷰 (재시작 후에도 콜백 유지)
class MultiTicketPanelRootView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        # 슬롯 1~3의 custom_id를 미리 고정 등록
        self.add_item(MultiOpenButton(1, label="열기(1)"))
        self.add_item(MultiOpenButton(2, label="열기(2)", style=discord.ButtonStyle.secondary))
        self.add_item(MultiOpenButton(3, label="열기(3)", style=discord.ButtonStyle.success))

# 메시지에 실제로 붙는 뷰(라벨/스타일은 메시지용으로만 사용 — 콜백은 루트 뷰가 처리)
class MultiTicketPanelViewForMessage(discord.ui.View):
    def __init__(self, buttons: List[dict]):
        """
        buttons: [{"slot":1,"label":"문의함 열기","style":"primary"}, ...]
        """
        super().__init__(timeout=None)
        for b in buttons:
            slot = int(b["slot"])
            label = b.get("label") or "티켓 열기"
            style = _parse_style(b.get("style"))
            self.add_item(MultiOpenButton(slot, label=label, style=style))

# ---------- Cog ----------
class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    ticket = app_commands.Group(name="ticket", description="티켓 시스템 설정/사용")

    async def cog_load(self):
        # 퍼시스턴트 뷰(버튼 custom_id 기준) 등록
        self.bot.add_view(TicketPanelView())           # 레거시 단일 버튼
        self.bot.add_view(MultiTicketPanelRootView())  # 멀티 버튼
        self.bot.add_view(TicketControlsView())
        self.bot.add_view(ClosedTicketControlsView())

    # ---- 자동완성: preset 이름 ----
    async def _preset_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        names = _list_presets(interaction.guild.id) if interaction.guild else []
        current = (current or "").lower()
        if current:
            names = [n for n in names if current in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in names[:20]]

    # /ticket setup : 프리셋 생성/업데이트
    @ticket.command(name="setup", description="지원 역할/카테고리/로그 채널/채널명 패턴을 프리셋으로 저장합니다.")
    @app_commands.describe(
        preset="프리셋 이름(예: default, support, sales)",
        support_role="지원 담당 역할",
        category="티켓을 생성할 카테고리(없으면 최상단 생성)",
        logs_channel="트랜스크립트를 보낼 로그 채널(없으면 에페메럴로 파일 제공)",
        naming="채널명 패턴 (기본: ticket-{id}, 사용가능: {id}, {user})",
        make_default="이 프리셋을 기본값으로 지정"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ticket_setup(
        self, interaction: discord.Interaction,
        preset: Optional[str] = "default",
        support_role: Optional[discord.Role] = None,
        category: Optional[discord.CategoryChannel] = None,
        logs_channel: Optional[discord.TextChannel] = None,
        naming: Optional[str] = None,
        make_default: Optional[bool] = False,
    ):
        if not preset:
            preset = "default"
        cur = _get_preset_cfg(interaction.guild.id, preset) or {}
        if support_role is not None: cur["support_role_id"] = support_role.id
        if category is not None:     cur["category_id"] = category.id
        if logs_channel is not None: cur["logs_channel_id"] = logs_channel.id
        if naming is not None:       cur["naming"] = naming[:64]
        cur.setdefault("naming", "ticket-{id}")

        missing = []
        if "support_role_id" not in cur:
            missing.append("support_role")
        note = "" if not missing else " (※ 지금 당장 사용하려면 다음 값을 꼭 지정하세요: " + ", ".join(missing) + ")"

        _set_preset_cfg(interaction.guild.id, preset, cur, make_default=bool(make_default))

        sr = f"<@&{cur['support_role_id']}>" if "support_role_id" in cur else "미지정"
        cat = f"<#{cur['category_id']}>" if "category_id" in cur else "없음"
        log = f"<#{cur['logs_channel_id']}>" if "logs_channel_id" in cur else "없음"
        name_pat = cur.get("naming", "ticket-{id}")
        default_mark = " (기본값)" if bool(make_default) else ""

        await interaction.response.send_message(
            f"✅ 프리셋 **{preset}** 저장{default_mark}\n"
            f"- 지원 역할: {sr}\n- 카테고리: {cat}\n- 로그 채널: {log}\n- 채널명 패턴: `{name_pat}`\n{note}",
            ephemeral=True
        )

    # /ticket panel : (레거시) 단일 버튼 패널 전송
    @ticket.command(name="panel", description="선택한 프리셋으로 단일 버튼 티켓 패널을 전송합니다.")
    @app_commands.describe(
        preset="사용할 프리셋 이름(미지정 시 기본 프리셋)",
        title="임베드 제목 (예: 고객 지원 센터)",
        description="설명 (예: 아래 버튼을 눌러 문의를 시작하세요)"
    )
    @app_commands.autocomplete(preset=_preset_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ticket_panel(
        self, interaction: discord.Interaction,
        preset: Optional[str] = None,
        title: Optional[str] = "🎫 고객 지원",
        description: Optional[str] = "문제를 신고하거나 문의하려면 아래 **티켓 열기** 버튼을 누르세요."
    ):
        cfg = _get_preset_cfg(interaction.guild.id, preset)
        if not cfg or not cfg.get("support_role_id"):
            presets = _list_presets(interaction.guild.id)
            preset_hint = f"가능한 프리셋: {', '.join(presets) or '없음'}"
            return await interaction.response.send_message(
                f"먼저 `/ticket setup`으로 유효한 프리셋을 설정해 주세요. ({preset_hint})",
                ephemeral=True
            )

        emb = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        view = TicketPanelView()
        msg = await interaction.channel.send(embed=emb, view=view)
        _register_panel_single(msg.id, interaction.guild.id, interaction.channel.id, preset or (_guild_cfg(interaction.guild.id).get("default") or "default"))
        await interaction.response.send_message("✅ 패널을 전송했어요.", ephemeral=True)

    # /ticket panel_multi : 다중 버튼 패널 전송
    @ticket.command(name="panel_multi", description="여러 버튼(문의/신고/연합문의 등)으로 티켓 패널을 전송합니다.")
    @app_commands.describe(
        title="임베드 제목",
        description="패널 설명",
        preset1="슬롯1 프리셋 (예: support)",
        label1="슬롯1 버튼 라벨 (예: 문의함 열기)",
        style1="슬롯1 버튼 스타일 (primary/secondary/success/danger)",
        preset2="슬롯2 프리셋 (선택)",
        label2="슬롯2 버튼 라벨",
        style2="슬롯2 버튼 스타일",
        preset3="슬롯3 프리셋 (선택)",
        label3="슬롯3 버튼 라벨",
        style3="슬롯3 버튼 스타일",
    )
    @app_commands.autocomplete(preset1=_preset_autocomplete, preset2=_preset_autocomplete, preset3=_preset_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ticket_panel_multi(
        self, interaction: discord.Interaction,
        title: Optional[str] = "🎫 고객 지원",
        description: Optional[str] = "필요한 항목의 버튼을 눌러 티켓을 생성하세요.",
        preset1: Optional[str] = None,
        label1: Optional[str] = "문의함 열기",
        style1: Optional[str] = "primary",
        preset2: Optional[str] = None,
        label2: Optional[str] = "신고함 열기",
        style2: Optional[str] = "danger",
        preset3: Optional[str] = None,
        label3: Optional[str] = "연합문의 열기",
        style3: Optional[str] = "secondary",
    ):
        # 각 프리셋 유효성 확인(있는 것만 버튼 생성)
        slots: List[dict] = []
        mapping: Dict[str, Dict[str, str]] = {}

        def _add(slot: int, preset: Optional[str], label: Optional[str], style: Optional[str]):
            if not preset:
                return
            cfg = _get_preset_cfg(interaction.guild.id, preset)
            if not cfg or not cfg.get("support_role_id"):
                return
            slots.append({"slot": slot, "label": label or "티켓 열기", "style": style or "primary"})
            mapping[str(slot)] = {"preset": str(preset)}

        _add(1, preset1, label1, style1)
        _add(2, preset2, label2, style2)
        _add(3, preset3, label3, style3)

        if not slots:
            return await interaction.response.send_message(
                "유효한 프리셋이 없습니다. 먼저 `/ticket setup`으로 프리셋을 만들거나 올바르게 지정해 주세요.",
                ephemeral=True
            )

        emb = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        view_for_msg = MultiTicketPanelViewForMessage(buttons=slots)
        msg = await interaction.channel.send(embed=emb, view=view_for_msg)

        # 패널 매핑 저장(슬롯→프리셋)
        _register_panel_multi(msg.id, interaction.guild.id, interaction.channel.id, mapping)
        await interaction.response.send_message("✅ 다중 버튼 패널을 전송했어요.", ephemeral=True)

    # /ticket presets : 프리셋 목록
    @ticket.command(name="presets", description="저장된 프리셋들을 보여줍니다.")
    async def ticket_presets(self, interaction: discord.Interaction):
        names = _list_presets(interaction.guild.id)
        g = _guild_cfg(interaction.guild.id)
        default_name = g.get("default")
        if not names:
            return await interaction.response.send_message("아직 저장된 프리셋이 없습니다. `/ticket setup`으로 만들어 주세요.", ephemeral=True)
        lines = []
        for n in names:
            mark = " (기본)" if n == default_name else ""
            cfg = _get_preset_cfg(interaction.guild.id, n) or {}
            sr = f"<@&{cfg['support_role_id']}>" if "support_role_id" in cfg else "미지정"
            cat = f"<#{cfg['category_id']}>" if "category_id" in cfg else "없음"
            log = f"<#{cfg['logs_channel_id']}>" if "logs_channel_id" in cfg else "없음"
            name_pat = cfg.get("naming", "ticket-{id}")
            lines.append(f"• **{n}**{mark} — 역할:{sr}, 카테고리:{cat}, 로그:{log}, 패턴:`{name_pat}`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))