from __future__ import annotations

import time
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import DATA_DIR, load_json, save_json


AUTORESPONDER_CFG = DATA_DIR / "autoresponder_config.json"

DEFAULT_HEADER = "자동 응답"
DEFAULT_BODY = "{user}님, 요청하신 안내입니다."
DEFAULT_FOOTER = ""
DEFAULT_COLOR = "#F7B2D9"
DEFAULT_IMAGE_URL = ""

MAX_RULES_PER_GUILD = 50
RESPONSE_COOLDOWN_SECONDS = 5.0

MATCH_MODE_LABELS = {
    "contains": "포함",
    "exact": "정확히 일치",
    "startswith": "시작 문구",
}

RESPONSE_TYPE_LABELS = {
    "embed": "임베드",
    "text": "일반 텍스트",
}

MATCH_CHOICES = [
    app_commands.Choice(name="포함", value="contains"),
    app_commands.Choice(name="정확히 일치", value="exact"),
    app_commands.Choice(name="시작 문구", value="startswith"),
]

RESPONSE_TYPE_CHOICES = [
    app_commands.Choice(name="임베드", value="embed"),
    app_commands.Choice(name="일반 텍스트", value="text"),
]

AUTORESPONDER_VARIABLES: tuple[tuple[str, str], ...] = (
    ("{mention}", "메시지를 보낸 유저를 멘션합니다."),
    ("{user}", "메시지를 보낸 유저를 멘션합니다. `{mention}`과 같습니다."),
    ("{username}", "서버에서 보이는 이름/닉네임입니다."),
    ("{name}", "디스코드 계정 이름입니다."),
    ("{tag}", "유저 태그 문자열입니다."),
    ("{id}", "유저 ID입니다."),
    ("{server}", "서버 이름입니다."),
    ("{guild}", "서버 이름입니다. `{server}`와 같습니다."),
    ("{channel}", "메시지가 작성된 채널을 멘션합니다."),
    ("{channel_name}", "메시지가 작성된 채널 이름입니다."),
    ("{message}", "유저가 작성한 메시지 내용입니다."),
    ("{trigger}", "자동응답 트리거 문구입니다."),
    ("{created_at}", "메시지 작성 시간입니다."),
)


class AutoResponderConfigError(ValueError):
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
        raise AutoResponderConfigError("색상은 `#F7B2D9`처럼 6자리 HEX 값으로 입력해 주세요.")
    try:
        return int(raw, 16)
    except ValueError as exc:
        raise AutoResponderConfigError("색상은 `#F7B2D9`처럼 6자리 HEX 값으로 입력해 주세요.") from exc


def _as_color_text(value: object) -> str:
    try:
        return f"#{_parse_color(value):06X}"
    except AutoResponderConfigError:
        return DEFAULT_COLOR


def _normalize_image_url(value: object) -> str:
    """Discord 임베드 하단 이미지/GIF로 사용할 URL을 정리합니다. 빈 값이면 사용하지 않습니다."""
    raw = str(value or "").replace("\x00", "").strip()
    if not raw:
        return ""

    raw = _cut(raw, 2048)
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AutoResponderConfigError("하단 이미지/GIF는 `https://...` 형태의 이미지 URL로 입력해 주세요. 비워두면 사용하지 않습니다.")
    return raw


def _variables_text() -> str:
    return "\n".join(f"`{name}` — {description}" for name, description in AUTORESPONDER_VARIABLES)


def _variables_inline() -> str:
    return " ".join(f"`{name}`" for name, _ in AUTORESPONDER_VARIABLES)


def _response_type(rule: dict[str, Any]) -> str:
    value = str(rule.get("response_type") or "embed").lower().strip()
    return value if value in RESPONSE_TYPE_LABELS else "embed"


def _safe_member_name(user: discord.abc.User) -> str:
    if isinstance(user, discord.Member):
        return user.display_name
    return user.name


def _channel_name(channel: discord.abc.Messageable) -> str:
    return getattr(channel, "name", "알 수 없음")


def _render_template(template: str, *, message: Optional[discord.Message], rule: dict[str, Any]) -> str:
    if message is None or message.guild is None:
        replacements = {
            "{mention}": "@user",
            "{user}": "@user",
            "{username}": "사용자",
            "{name}": "user",
            "{tag}": "user",
            "{id}": "0",
            "{server}": "서버",
            "{guild}": "서버",
            "{channel}": "#channel",
            "{channel_name}": "channel",
            "{message}": rule.get("trigger") or "",
            "{trigger}": rule.get("trigger") or "",
            "{created_at}": "지금",
        }
    else:
        author = message.author
        guild = message.guild
        channel = message.channel
        channel_mention = getattr(channel, "mention", f"#{_channel_name(channel)}")
        replacements = {
            "{mention}": author.mention,
            "{user}": author.mention,
            "{username}": _safe_member_name(author),
            "{name}": author.name,
            "{tag}": str(author),
            "{id}": str(author.id),
            "{server}": guild.name,
            "{guild}": guild.name,
            "{channel}": channel_mention,
            "{channel_name}": _channel_name(channel),
            "{message}": message.content,
            "{trigger}": str(rule.get("trigger") or ""),
            "{created_at}": discord.utils.format_dt(message.created_at, style="F"),
        }

    rendered = str(template or "")
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)
    return rendered


def _default_rule(
    *,
    trigger: str,
    channel_id: Optional[int],
    match_mode: str = "contains",
    ignore_case: bool = True,
    response_type: str = "embed",
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:8],
        "enabled": True,
        "trigger": trigger,
        "channel_id": channel_id,
        "match_mode": match_mode if match_mode in MATCH_MODE_LABELS else "contains",
        "ignore_case": bool(ignore_case),
        "response_type": response_type if response_type in RESPONSE_TYPE_LABELS else "embed",
        "header": DEFAULT_HEADER,
        "body": DEFAULT_BODY,
        "footer": DEFAULT_FOOTER,
        "color": DEFAULT_COLOR,
        "image_url": DEFAULT_IMAGE_URL,
    }


def _build_embed(rule: dict[str, Any], *, message: Optional[discord.Message] = None) -> discord.Embed:
    header = _cut(_render_template(rule.get("header") or DEFAULT_HEADER, message=message, rule=rule), 256)
    body = _cut(_render_template(rule.get("body") or DEFAULT_BODY, message=message, rule=rule), 4096)
    footer = _cut(_render_template(rule.get("footer") or "", message=message, rule=rule), 2048)
    color = _parse_color(rule.get("color"), fallback=DEFAULT_COLOR)
    image_url = _normalize_image_url(rule.get("image_url"))

    embed = discord.Embed(title=header, description=body, color=color)
    if message is not None:
        embed.set_thumbnail(url=message.author.display_avatar.url)
    if image_url:
        embed.set_image(url=image_url)
    if footer:
        embed.set_footer(text=footer)
    return embed


def _build_text_response(rule: dict[str, Any], *, message: Optional[discord.Message] = None) -> str:
    body = _render_template(rule.get("body") or DEFAULT_BODY, message=message, rule=rule)
    return _cut(body.strip() or DEFAULT_BODY, 2000)


def _preview_embed_for_text(rule: dict[str, Any], *, message: Optional[discord.Message] = None) -> discord.Embed:
    preview = _build_text_response(rule, message=message)
    embed = discord.Embed(
        title="자동응답 일반 텍스트 미리보기",
        description=_cut(preview, 4096),
        color=_parse_color(DEFAULT_COLOR),
    )
    embed.set_footer(text="실제 자동응답은 임베드가 아니라 일반 메시지로 전송됩니다.")
    return embed


def _rule_summary(rule: dict[str, Any], guild: Optional[discord.Guild] = None) -> str:
    channel_id = rule.get("channel_id")
    if channel_id and guild:
        channel = guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
        channel_text = channel.mention if channel else f"삭제된 채널 `{channel_id}`"
    elif channel_id:
        channel_text = f"채널 `{channel_id}`"
    else:
        channel_text = "모든 채널"

    mode = MATCH_MODE_LABELS.get(str(rule.get("match_mode") or "contains"), "포함")
    response_type = RESPONSE_TYPE_LABELS.get(_response_type(rule), "임베드")
    enabled = "켜짐" if rule.get("enabled", True) else "꺼짐"
    ignore_case = "무시" if rule.get("ignore_case", True) else "구분"
    image_part = ""
    if _response_type(rule) == "embed":
        image = "있음" if rule.get("image_url") else "없음"
        image_part = f" / 이미지: {image}"
    return (
        f"`{rule.get('id')}` — **{_cut(str(rule.get('trigger') or ''), 60)}**\n"
        f"상태: {enabled} / 응답: {response_type} / 방식: {mode} / 대소문자: {ignore_case} / 채널: {channel_text}{image_part}"
    )


def _message_matches(rule: dict[str, Any], message: discord.Message) -> bool:
    trigger = str(rule.get("trigger") or "").strip()
    if not trigger:
        return False

    if rule.get("channel_id"):
        try:
            if int(rule["channel_id"]) != message.channel.id:
                return False
        except (TypeError, ValueError):
            return False

    content = message.content or ""
    if rule.get("ignore_case", True):
        trigger_cmp = trigger.casefold()
        content_cmp = content.casefold()
    else:
        trigger_cmp = trigger
        content_cmp = content

    mode = str(rule.get("match_mode") or "contains")
    if mode == "exact":
        return content_cmp.strip() == trigger_cmp
    if mode == "startswith":
        return content_cmp.startswith(trigger_cmp)
    return trigger_cmp in content_cmp


class AutoResponseModal(discord.ui.Modal):
    def __init__(self, cog: "AutoResponder", rule: dict[str, Any], *, is_edit: bool = False):
        self.response_type = _response_type(rule)
        title = "자동응답 임베드 설정" if self.response_type == "embed" else "자동응답 일반 텍스트 설정"
        super().__init__(title=title, timeout=300)
        self.cog = cog
        self.rule = rule
        self.is_edit = is_edit

        if self.response_type == "text":
            self.body = discord.ui.TextInput(
                label="응답 내용",
                style=discord.TextStyle.paragraph,
                placeholder="예: {user}님, 아래 내용을 확인해 주세요! ✨",
                default=_cut(str(rule.get("body") or DEFAULT_BODY), 2000),
                required=True,
                max_length=2000,
            )
            self.add_item(self.body)
            return

        self.header = discord.ui.TextInput(
            label="헤더 / 제목",
            style=discord.TextStyle.short,
            placeholder="예: 안내 메시지",
            default=_cut(str(rule.get("header") or DEFAULT_HEADER), 256),
            required=True,
            max_length=256,
        )
        self.body = discord.ui.TextInput(
            label="중앙 내용",
            style=discord.TextStyle.paragraph,
            placeholder="예: {user}님, 아래 내용을 확인해 주세요! ✨",
            default=_cut(str(rule.get("body") or DEFAULT_BODY), 4000),
            required=True,
            max_length=4000,
        )
        self.footer = discord.ui.TextInput(
            label="아래 푸터",
            style=discord.TextStyle.paragraph,
            placeholder="예: 문의가 있으면 관리자에게 알려 주세요.",
            default=_cut(str(rule.get("footer") or DEFAULT_FOOTER), 2048),
            required=False,
            max_length=2048,
        )
        self.color = discord.ui.TextInput(
            label="임베드 색상 HEX",
            style=discord.TextStyle.short,
            placeholder="#F7B2D9",
            default=_as_color_text(rule.get("color", DEFAULT_COLOR)),
            required=False,
            max_length=7,
        )
        self.image_url = discord.ui.TextInput(
            label="하단 이미지/GIF URL",
            style=discord.TextStyle.short,
            placeholder="예: https://example.com/guide.gif / 비워두면 없음",
            default=_cut(str(rule.get("image_url") or DEFAULT_IMAGE_URL), 2048),
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

        if self.response_type == "text":
            self.rule.update({
                "response_type": "text",
                "body": _normalize_text(self.body.value, fallback=DEFAULT_BODY, limit=2000),
            })
        else:
            try:
                color_text = f"#{_parse_color(self.color.value, fallback=DEFAULT_COLOR):06X}"
                image_url = _normalize_image_url(self.image_url.value)
            except AutoResponderConfigError as exc:
                return await interaction.response.send_message(str(exc), ephemeral=True)

            self.rule.update({
                "response_type": "embed",
                "header": _normalize_text(self.header.value, fallback=DEFAULT_HEADER, limit=256),
                "body": _normalize_text(self.body.value, fallback=DEFAULT_BODY, limit=4000),
                "footer": _normalize_text(self.footer.value, fallback="", limit=2048),
                "color": color_text,
                "image_url": image_url,
            })

        if self.is_edit:
            saved = self.cog.update_rule(interaction.guild.id, self.rule)
        else:
            saved = self.cog.add_rule(interaction.guild.id, self.rule)

        if not saved:
            return await interaction.response.send_message(
                f"자동응답은 서버당 최대 {MAX_RULES_PER_GUILD}개까지 등록할 수 있습니다.",
                ephemeral=True,
            )

        embed = _build_embed(self.rule) if _response_type(self.rule) == "embed" else _preview_embed_for_text(self.rule)
        embed.add_field(name="트리거", value=f"`{_cut(str(self.rule.get('trigger') or ''), 100)}`", inline=False)
        embed.add_field(name="규칙 ID", value=f"`{self.rule.get('id')}`", inline=True)
        embed.add_field(name="응답 형식", value=RESPONSE_TYPE_LABELS.get(_response_type(self.rule), "임베드"), inline=True)
        embed.add_field(name="사용 가능 변수", value=_variables_inline(), inline=False)

        action = "수정" if self.is_edit else "추가"
        await interaction.response.send_message(
            f"✅ 자동응답 규칙을 {action}했습니다. 아래는 미리보기입니다.",
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        message = f"자동응답 설정 중 오류가 발생했습니다.\n```{type(error).__name__}: {error}```"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class AutoResponder(commands.Cog):
    """특정 문구가 감지되면 자동응답을 전송합니다."""

    autoresponder = app_commands.Group(name="자동응답", description="특정 문구에 자동으로 응답을 보냅니다.")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[str, list[dict[str, Any]]] = load_json(AUTORESPONDER_CFG, {})
        if not isinstance(self.config, dict):
            self.config = {}
        self._cooldowns: dict[tuple[int, int, int, str], float] = {}

    def _save(self) -> None:
        save_json(AUTORESPONDER_CFG, self.config)

    def get_rules(self, guild_id: int) -> list[dict[str, Any]]:
        rules = self.config.get(str(guild_id), [])
        return rules if isinstance(rules, list) else []

    def find_rule(self, guild_id: int, rule_id: str) -> Optional[dict[str, Any]]:
        rule_id = str(rule_id or "").strip()
        for rule in self.get_rules(guild_id):
            if str(rule.get("id")) == rule_id:
                return rule
        return None

    def add_rule(self, guild_id: int, rule: dict[str, Any]) -> bool:
        key = str(guild_id)
        rules = self.get_rules(guild_id)
        if len(rules) >= MAX_RULES_PER_GUILD:
            return False
        rules.append(rule)
        self.config[key] = rules
        self._save()
        return True

    def update_rule(self, guild_id: int, updated: dict[str, Any]) -> bool:
        key = str(guild_id)
        rules = self.get_rules(guild_id)
        for idx, rule in enumerate(rules):
            if str(rule.get("id")) == str(updated.get("id")):
                rules[idx] = updated
                self.config[key] = rules
                self._save()
                return True
        return False

    def delete_rule(self, guild_id: int, rule_id: str) -> bool:
        key = str(guild_id)
        rules = self.get_rules(guild_id)
        new_rules = [rule for rule in rules if str(rule.get("id")) != str(rule_id)]
        if len(new_rules) == len(rules):
            return False
        self.config[key] = new_rules
        self._save()
        return True

    def _cooldown_key(self, message: discord.Message, rule: dict[str, Any]) -> tuple[int, int, int, str]:
        guild_id = message.guild.id if message.guild else 0
        return (guild_id, message.channel.id, message.author.id, str(rule.get("id")))

    def _is_on_cooldown(self, message: discord.Message, rule: dict[str, Any]) -> bool:
        key = self._cooldown_key(message, rule)
        now = time.monotonic()
        previous = self._cooldowns.get(key, 0.0)
        if now - previous < RESPONSE_COOLDOWN_SECONDS:
            return True
        self._cooldowns[key] = now
        if len(self._cooldowns) > 5000:
            cutoff = now - 60.0
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if v >= cutoff}
        return False

    @autoresponder.command(name="추가", description="자동응답 트리거와 응답 내용을 추가합니다.")
    @app_commands.describe(
        trigger="자동응답을 실행할 문구",
        channel="이 채널에서만 반응하게 합니다. 비워두면 모든 채널에서 반응합니다.",
        match_mode="트리거 문구를 비교하는 방식",
        ignore_case="켜짐: 대소문자 무시, 꺼짐: 대소문자 구분",
        response_type="응답을 임베드로 보낼지 일반 텍스트로 보낼지 선택합니다.",
    )
    @app_commands.rename(trigger="트리거", channel="채널", match_mode="일치방식", ignore_case="대소문자무시", response_type="응답형식")
    @app_commands.choices(match_mode=MATCH_CHOICES, response_type=RESPONSE_TYPE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add(
        self,
        interaction: discord.Interaction,
        trigger: str,
        channel: Optional[discord.TextChannel] = None,
        match_mode: Optional[app_commands.Choice[str]] = None,
        ignore_case: Optional[bool] = True,
        response_type: Optional[app_commands.Choice[str]] = None,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        trigger = _normalize_text(trigger, fallback="", limit=100)
        if not trigger:
            return await interaction.response.send_message("트리거 문구를 입력해 주세요.", ephemeral=True)

        if len(self.get_rules(interaction.guild.id)) >= MAX_RULES_PER_GUILD:
            return await interaction.response.send_message(
                f"자동응답은 서버당 최대 {MAX_RULES_PER_GUILD}개까지 등록할 수 있습니다.",
                ephemeral=True,
            )

        rule = _default_rule(
            trigger=trigger,
            channel_id=channel.id if channel else None,
            match_mode=match_mode.value if match_mode else "contains",
            ignore_case=True if ignore_case is None else bool(ignore_case),
            response_type=response_type.value if response_type else "embed",
        )
        await interaction.response.send_modal(AutoResponseModal(self, rule, is_edit=False))

    @autoresponder.command(name="수정", description="기존 자동응답 내용을 수정합니다.")
    @app_commands.describe(rule_id="/자동응답 목록에서 확인한 규칙 ID", response_type="응답 형식을 바꾸려면 선택합니다. 비워두면 기존 형식을 유지합니다.")
    @app_commands.rename(rule_id="규칙id", response_type="응답형식")
    @app_commands.choices(response_type=RESPONSE_TYPE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def edit(self, interaction: discord.Interaction, rule_id: str, response_type: Optional[app_commands.Choice[str]] = None):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        rule = self.find_rule(interaction.guild.id, rule_id)
        if not rule:
            return await interaction.response.send_message("해당 ID의 자동응답 규칙을 찾을 수 없습니다.", ephemeral=True)

        editable_rule = dict(rule)
        if response_type is not None:
            editable_rule["response_type"] = response_type.value
        await interaction.response.send_modal(AutoResponseModal(self, editable_rule, is_edit=True))

    @autoresponder.command(name="목록", description="등록된 자동응답 목록을 확인합니다.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_rules(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        rules = self.get_rules(interaction.guild.id)
        if not rules:
            return await interaction.response.send_message("등록된 자동응답이 없습니다. `/자동응답 추가`를 먼저 사용해 주세요.", ephemeral=True)

        chunks: list[str] = []
        for rule in rules[:20]:
            chunks.append(_rule_summary(rule, interaction.guild))
        if len(rules) > 20:
            chunks.append(f"…외 {len(rules) - 20}개")

        embed = discord.Embed(
            title=f"자동응답 목록 ({len(rules)}개)",
            description="\n\n".join(chunks),
            color=_parse_color(DEFAULT_COLOR),
        )
        embed.set_footer(text="수정/삭제/테스트에는 목록에 표시된 규칙 ID를 사용합니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @autoresponder.command(name="보기", description="자동응답 규칙 하나를 자세히 확인합니다.")
    @app_commands.describe(rule_id="/자동응답 목록에서 확인한 규칙 ID")
    @app_commands.rename(rule_id="규칙id")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def show(self, interaction: discord.Interaction, rule_id: str):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        rule = self.find_rule(interaction.guild.id, rule_id)
        if not rule:
            return await interaction.response.send_message("해당 ID의 자동응답 규칙을 찾을 수 없습니다.", ephemeral=True)

        embed = _build_embed(rule) if _response_type(rule) == "embed" else _preview_embed_for_text(rule)
        embed.add_field(name="규칙", value=_rule_summary(rule, interaction.guild), inline=False)
        embed.add_field(name="변수 목록", value="`/자동응답 변수` 명령어로 확인할 수 있습니다.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @autoresponder.command(name="삭제", description="자동응답 규칙을 삭제합니다.")
    @app_commands.describe(rule_id="/자동응답 목록에서 확인한 규칙 ID")
    @app_commands.rename(rule_id="규칙id")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def delete(self, interaction: discord.Interaction, rule_id: str):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        if not self.delete_rule(interaction.guild.id, rule_id):
            return await interaction.response.send_message("해당 ID의 자동응답 규칙을 찾을 수 없습니다.", ephemeral=True)
        await interaction.response.send_message(f"✅ 자동응답 규칙 `{rule_id}`을 삭제했습니다.", ephemeral=True)

    @autoresponder.command(name="켜기", description="자동응답 규칙을 켭니다.")
    @app_commands.describe(rule_id="/자동응답 목록에서 확인한 규칙 ID")
    @app_commands.rename(rule_id="규칙id")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def enable(self, interaction: discord.Interaction, rule_id: str):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        rule = self.find_rule(interaction.guild.id, rule_id)
        if not rule:
            return await interaction.response.send_message("해당 ID의 자동응답 규칙을 찾을 수 없습니다.", ephemeral=True)
        rule["enabled"] = True
        self.update_rule(interaction.guild.id, rule)
        await interaction.response.send_message(f"✅ 자동응답 규칙 `{rule_id}`을 켰습니다.", ephemeral=True)

    @autoresponder.command(name="끄기", description="자동응답 규칙을 끕니다.")
    @app_commands.describe(rule_id="/자동응답 목록에서 확인한 규칙 ID")
    @app_commands.rename(rule_id="규칙id")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction, rule_id: str):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        rule = self.find_rule(interaction.guild.id, rule_id)
        if not rule:
            return await interaction.response.send_message("해당 ID의 자동응답 규칙을 찾을 수 없습니다.", ephemeral=True)
        rule["enabled"] = False
        self.update_rule(interaction.guild.id, rule)
        await interaction.response.send_message(f"✅ 자동응답 규칙 `{rule_id}`을 껐습니다.", ephemeral=True)

    @autoresponder.command(name="테스트", description="자동응답을 비공개로 미리 봅니다.")
    @app_commands.describe(rule_id="/자동응답 목록에서 확인한 규칙 ID", sample_message="변수 {message}에 넣을 테스트 메시지")
    @app_commands.rename(rule_id="규칙id", sample_message="테스트메시지")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def test(self, interaction: discord.Interaction, rule_id: str, sample_message: Optional[str] = None):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        rule = self.find_rule(interaction.guild.id, rule_id)
        if not rule:
            return await interaction.response.send_message("해당 ID의 자동응답 규칙을 찾을 수 없습니다.", ephemeral=True)

        # 실제 메시지 객체를 만들 수 없으므로, 트리거/기본값 기반 미리보기를 보여줍니다.
        preview_rule = dict(rule)
        if sample_message:
            preview_rule["trigger"] = sample_message
        embed = _build_embed(preview_rule) if _response_type(preview_rule) == "embed" else _preview_embed_for_text(preview_rule)
        embed.add_field(name="참고", value="실제 채팅에서 작동하면 유저/채널/메시지 변수는 실제 값으로 치환됩니다.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @autoresponder.command(name="변수", description="자동응답에서 사용할 수 있는 변수 목록을 확인합니다.")
    async def variables(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        embed = discord.Embed(
            title="자동응답 변수 목록",
            description=_variables_text(),
            color=_parse_color(DEFAULT_COLOR),
        )
        embed.set_footer(text="임베드 모드에서는 헤더/중앙 내용/푸터, 일반 텍스트 모드에서는 응답 내용에서 사용할 수 있습니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if not message.content:
            return

        rules = self.get_rules(message.guild.id)
        if not rules:
            return

        for rule in rules:
            if not rule.get("enabled", True):
                continue
            if not _message_matches(rule, message):
                continue
            if self._is_on_cooldown(message, rule):
                return

            try:
                if _response_type(rule) == "text":
                    await message.channel.send(
                        content=_build_text_response(rule, message=message),
                        allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
                    )
                else:
                    embed = _build_embed(rule, message=message)
                    await message.channel.send(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
                    )
            except discord.Forbidden:
                print(f"[autoresponder] missing permission: guild={message.guild.id}, channel={message.channel.id}")
            except discord.HTTPException as exc:
                print(f"[autoresponder] send failed: guild={message.guild.id}, {type(exc).__name__}: {exc}")
            return


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoResponder(bot))
