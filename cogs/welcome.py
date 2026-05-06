from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import DATA_DIR, load_json, save_json


WELCOME_CFG = DATA_DIR / "welcome_config.json"

DEFAULT_HEADER = "어서 와요, {username}님!"
DEFAULT_BODY = "{mention}님이 **{server}**에 들어왔어요. 🎉\n현재 서버 인원: **{count}명**"
DEFAULT_FOOTER = "즐거운 시간 보내세요!"
DEFAULT_COLOR = "#F7B2D9"
DEFAULT_IMAGE_URL = ""

WELCOME_VARIABLES: tuple[tuple[str, str], ...] = (
    ("{mention}", "새 멤버를 멘션합니다."),
    ("{user}", "새 멤버를 멘션합니다. `{mention}`과 같습니다."),
    ("{username}", "서버에서 보이는 이름/닉네임입니다."),
    ("{name}", "디스코드 계정 이름입니다."),
    ("{tag}", "유저 태그 문자열입니다."),
    ("{id}", "유저 ID입니다."),
    ("{server}", "서버 이름입니다."),
    ("{guild}", "서버 이름입니다. `{server}`와 같습니다."),
    ("{count}", "현재 서버 멤버 수입니다."),
    ("{joined_at}", "서버 입장 시간입니다."),
    ("{created_at}", "계정 생성 시간입니다."),
)


class WelcomeConfigError(ValueError):
    pass


def _cut(value: str, limit: int) -> str:
    value = str(value or "")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _normalize_text(value: object, *, fallback: str, limit: int) -> str:
    """입력 텍스트를 Discord embed 제한에 맞춥니다. 이모지/커스텀 이모지는 건드리지 않습니다."""
    text = str(value or "").replace("\x00", "").strip()
    if not text:
        text = fallback
    return _cut(text, limit)


def _parse_color(value: object, *, fallback: str = DEFAULT_COLOR) -> int:
    raw = str(value or "").strip()
    if not raw:
        raw = fallback
    if raw.startswith("#"):
        raw = raw[1:]
    if raw.lower().startswith("0x"):
        raw = raw[2:]
    if len(raw) != 6:
        raise WelcomeConfigError("색상은 `#F7B2D9`처럼 6자리 HEX 값으로 입력해 주세요.")
    try:
        return int(raw, 16)
    except ValueError as exc:
        raise WelcomeConfigError("색상은 `#F7B2D9`처럼 6자리 HEX 값으로 입력해 주세요.") from exc


def _as_color_text(value: object) -> str:
    try:
        return f"#{_parse_color(value):06X}"
    except WelcomeConfigError:
        return DEFAULT_COLOR


def _normalize_image_url(value: object) -> str:
    """Discord 임베드 하단 이미지/GIF로 사용할 URL을 정리합니다. 빈 값이면 사용하지 않습니다."""
    raw = str(value or "").replace("\x00", "").strip()
    if not raw:
        return ""

    raw = _cut(raw, 2048)
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WelcomeConfigError("하단 이미지/GIF는 `https://...` 형태의 이미지 URL로 입력해 주세요. 비워두면 사용하지 않습니다.")
    return raw


def _variables_text() -> str:
    return "\n".join(f"`{name}` — {description}" for name, description in WELCOME_VARIABLES)


def _variables_inline() -> str:
    return " ".join(f"`{name}`" for name, _ in WELCOME_VARIABLES)


def _render_template(template: str, member: discord.Member) -> str:
    guild = member.guild
    joined_at = discord.utils.format_dt(member.joined_at, style="F") if member.joined_at else "알 수 없음"
    created_at = discord.utils.format_dt(member.created_at, style="F")

    replacements = {
        "{mention}": member.mention,
        "{user}": member.mention,
        "{username}": member.display_name,
        "{name}": member.name,
        "{tag}": str(member),
        "{id}": str(member.id),
        "{server}": guild.name,
        "{guild}": guild.name,
        "{count}": str(guild.member_count or len(guild.members)),
        "{joined_at}": joined_at,
        "{created_at}": created_at,
    }

    rendered = str(template or "")
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)
    return rendered


def _default_config(channel_id: Optional[int] = None) -> dict[str, Any]:
    return {
        "enabled": True,
        "channel_id": channel_id,
        "header": DEFAULT_HEADER,
        "body": DEFAULT_BODY,
        "footer": DEFAULT_FOOTER,
        "color": DEFAULT_COLOR,
        "image_url": DEFAULT_IMAGE_URL,
    }


def _build_embed(cfg: dict[str, Any], member: discord.Member) -> discord.Embed:
    header = _cut(_render_template(cfg.get("header") or DEFAULT_HEADER, member), 256)
    body = _cut(_render_template(cfg.get("body") or DEFAULT_BODY, member), 4096)
    footer = _cut(_render_template(cfg.get("footer") or "", member), 2048)
    color = _parse_color(cfg.get("color"), fallback=DEFAULT_COLOR)
    image_url = _normalize_image_url(cfg.get("image_url"))

    embed = discord.Embed(title=header, description=body, color=color)
    embed.set_thumbnail(url=member.display_avatar.url)
    if image_url:
        embed.set_image(url=image_url)
    if footer:
        embed.set_footer(text=footer)
    return embed


def _get_channel(guild: discord.Guild, channel_id: object) -> Optional[discord.abc.Messageable]:
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return None

    channel = guild.get_channel(cid)
    if isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel, discord.StageChannel)):
        return channel
    return None


class WelcomeMessageModal(discord.ui.Modal):
    def __init__(self, cog: "Welcome", channel: discord.TextChannel, current: Optional[dict[str, Any]] = None):
        super().__init__(title="환영 메시지 임베드 설정", timeout=300)
        self.cog = cog
        self.channel = channel

        cfg = current or _default_config(channel.id)

        self.header = discord.ui.TextInput(
            label="헤더 / 제목",
            style=discord.TextStyle.short,
            placeholder="예: 어서 와요, {username}님! 🎉",
            default=_cut(str(cfg.get("header") or DEFAULT_HEADER), 256),
            required=True,
            max_length=256,
        )
        self.body = discord.ui.TextInput(
            label="중앙 내용",
            style=discord.TextStyle.paragraph,
            placeholder="예: {mention}님, {server}에 오신 걸 환영해요! ✨",
            default=_cut(str(cfg.get("body") or DEFAULT_BODY), 4000),
            required=True,
            max_length=4000,
        )
        self.footer = discord.ui.TextInput(
            label="아래 푸터",
            style=discord.TextStyle.paragraph,
            placeholder="예: 즐거운 시간 보내세요!",
            default=_cut(str(cfg.get("footer") or DEFAULT_FOOTER), 2048),
            required=False,
            max_length=2048,
        )
        self.color = discord.ui.TextInput(
            label="임베드 색상 HEX",
            style=discord.TextStyle.short,
            placeholder="#F7B2D9",
            default=_as_color_text(cfg.get("color", DEFAULT_COLOR)),
            required=False,
            max_length=7,
        )
        self.image_url = discord.ui.TextInput(
            label="하단 이미지/GIF URL",
            style=discord.TextStyle.short,
            placeholder="예: https://example.com/welcome.gif / 비워두면 없음",
            default=_cut(str(cfg.get("image_url") or DEFAULT_IMAGE_URL), 2048),
            required=False,
            max_length=2048,
        )

        self.add_item(self.header)
        self.add_item(self.body)
        self.add_item(self.footer)
        self.add_item(self.color)
        self.add_item(self.image_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        try:
            color_text = f"#{_parse_color(self.color.value, fallback=DEFAULT_COLOR):06X}"
            image_url = _normalize_image_url(self.image_url.value)
        except WelcomeConfigError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)

        cfg = {
            "enabled": True,
            "channel_id": self.channel.id,
            "header": _normalize_text(self.header.value, fallback=DEFAULT_HEADER, limit=256),
            "body": _normalize_text(self.body.value, fallback=DEFAULT_BODY, limit=4000),
            "footer": _normalize_text(self.footer.value, fallback="", limit=2048),
            "color": color_text,
            "image_url": image_url,
        }
        self.cog.set_config(interaction.guild.id, cfg)

        embed = _build_embed(cfg, interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.me)
        embed.add_field(
            name="사용 가능 변수",
            value=_variables_inline(),
            inline=False,
        )

        await interaction.response.send_message(
            f"✅ 환영 메시지를 {self.channel.mention} 채널로 설정했습니다. 아래는 미리보기입니다.",
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        message = f"환영 메시지 설정 중 오류가 발생했습니다.\n```{type(error).__name__}: {error}```"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class Welcome(commands.Cog):
    """새 멤버가 들어오면 지정 채널에 임베드 환영 메시지를 보냅니다."""

    welcome = app_commands.Group(name="환영", description="새 멤버 환영 메시지를 설정합니다.")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[str, dict[str, Any]] = load_json(WELCOME_CFG, {})

    def _save(self) -> None:
        save_json(WELCOME_CFG, self.config)

    def get_config(self, guild_id: int) -> Optional[dict[str, Any]]:
        cfg = self.config.get(str(guild_id))
        return cfg if isinstance(cfg, dict) else None

    def set_config(self, guild_id: int, cfg: dict[str, Any]) -> None:
        self.config[str(guild_id)] = cfg
        self._save()

    @welcome.command(name="설정", description="환영 메시지를 보낼 채널과 임베드 내용을 설정합니다.")
    @app_commands.describe(channel="새 멤버 환영 메시지를 보낼 채널")
    @app_commands.rename(channel="채널")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        me = interaction.guild.me or await interaction.guild.fetch_member(interaction.client.user.id)
        perms = channel.permissions_for(me)
        if not perms.send_messages or not perms.embed_links:
            return await interaction.response.send_message(
                f"{channel.mention} 채널에서 봇에게 `메시지 보내기`와 `링크 임베드` 권한이 필요합니다.",
                ephemeral=True,
            )

        current = self.get_config(interaction.guild.id) or _default_config(channel.id)
        current["channel_id"] = channel.id
        await interaction.response.send_modal(WelcomeMessageModal(self, channel, current))

    @welcome.command(name="변수", description="환영 메시지에서 사용할 수 있는 변수 목록을 확인합니다.")
    async def variables(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        embed = discord.Embed(
            title="환영 메시지 변수 목록",
            description=_variables_text(),
            color=_parse_color(DEFAULT_COLOR),
        )
        embed.set_footer(text="헤더, 중앙 내용, 푸터에서 사용할 수 있습니다. 이미지/GIF URL에는 변수 사용을 권장하지 않습니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @welcome.command(name="보기", description="현재 환영 메시지 설정을 확인합니다.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def show(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        cfg = self.get_config(interaction.guild.id)
        if not cfg:
            return await interaction.response.send_message("아직 환영 메시지가 설정되지 않았습니다. `/환영 설정`을 먼저 사용해 주세요.", ephemeral=True)

        channel = _get_channel(interaction.guild, cfg.get("channel_id"))
        embed = _build_embed(cfg, interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.me)
        embed.add_field(name="전송 채널", value=channel.mention if channel else "채널을 찾을 수 없음", inline=False)
        embed.add_field(name="상태", value="켜짐" if cfg.get("enabled", True) else "꺼짐", inline=True)
        embed.add_field(name="하단 이미지/GIF", value=cfg.get("image_url") or "사용 안 함", inline=False)
        embed.add_field(name="변수 목록", value="`/환영 변수` 명령어로 확인할 수 있습니다.", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @welcome.command(name="테스트", description="현재 설정으로 환영 메시지를 테스트 전송합니다.")
    @app_commands.describe(user="미리보기에 사용할 유저. 비워두면 본인을 사용합니다.")
    @app_commands.rename(user="유저")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def test(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        cfg = self.get_config(interaction.guild.id)
        if not cfg:
            return await interaction.response.send_message("아직 환영 메시지가 설정되지 않았습니다. `/환영 설정`을 먼저 사용해 주세요.", ephemeral=True)

        channel = _get_channel(interaction.guild, cfg.get("channel_id"))
        if channel is None:
            return await interaction.response.send_message("설정된 환영 채널을 찾을 수 없습니다. `/환영 설정`을 다시 실행해 주세요.", ephemeral=True)

        member = user or interaction.user
        if not isinstance(member, discord.Member):
            member = interaction.guild.me or await interaction.guild.fetch_member(interaction.client.user.id)

        embed = _build_embed(cfg, member)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.Forbidden:
            return await interaction.response.send_message("봇에게 환영 채널 메시지 전송 권한이 없습니다.", ephemeral=True)
        except discord.HTTPException as exc:
            return await interaction.response.send_message(f"테스트 전송에 실패했습니다.\n```{type(exc).__name__}: {exc}```", ephemeral=True)

        await interaction.response.send_message(f"✅ 테스트 환영 메시지를 {channel.mention}에 전송했습니다.", ephemeral=True)

    @welcome.command(name="끄기", description="새 멤버 환영 메시지 자동 전송을 끕니다.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        cfg = self.get_config(interaction.guild.id) or _default_config(None)
        cfg["enabled"] = False
        self.set_config(interaction.guild.id, cfg)
        await interaction.response.send_message("✅ 환영 메시지 자동 전송을 껐습니다.", ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = self.get_config(member.guild.id)
        if not cfg or not cfg.get("enabled", True):
            return

        channel = _get_channel(member.guild, cfg.get("channel_id"))
        if channel is None:
            return

        embed = _build_embed(cfg, member)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.Forbidden:
            print(f"[welcome] missing permission: guild={member.guild.id}, channel={cfg.get('channel_id')}")
        except discord.HTTPException as exc:
            print(f"[welcome] send failed: guild={member.guild.id}, {type(exc).__name__}: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
