from __future__ import annotations

from contextlib import contextmanager
import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.storage import DATA_DIR

DB_PATH = Path(DATA_DIR / "leveling.db")

LIGHT_OUTLINE = (235, 238, 245)

LEVEL_TYPE_CHOICES = [
    app_commands.Choice(name="전체", value="total"),
    app_commands.Choice(name="채팅", value="text"),
    app_commands.Choice(name="음성", value="voice"),
]

CONFIG_LEVEL_TYPE_CHOICES = [
    app_commands.Choice(name="채팅만", value="text"),
    app_commands.Choice(name="음성만", value="voice"),
    app_commands.Choice(name="채팅+음성", value="both"),
]

RANK_TYPE_NAMES = {
    "total": "전체",
    "text": "채팅",
    "voice": "음성",
}

LEVEL_MODE_NAMES = {
    "text": "채팅만",
    "voice": "음성만",
    "both": "채팅+음성",
}


@dataclass(slots=True)
class LevelConfig:
    enabled: bool = True
    level_type: str = "both"  # text, voice, both
    min_text_xp: int = 15
    max_text_xp: int = 25
    message_cooldown_seconds: int = 60
    voice_xp_per_minute: int = 10
    levelup_channel_id: Optional[int] = None
    levelup_message: str = "🎉 {user}님이 **레벨 {level}** 달성!"


def _now_ts() -> float:
    return time.time()


def _xp_for_next_level(current_level: int) -> int:
    """현재 레벨에서 다음 레벨까지 필요한 추가 XP."""
    level = max(0, int(current_level))
    return 100 + (level * 50) + (level * level * 5)


def _level_from_xp(total_xp: int) -> int:
    xp = max(0, int(total_xp))
    level = 0
    while xp >= _xp_for_next_level(level):
        xp -= _xp_for_next_level(level)
        level += 1
    return level


def _progress_in_level(total_xp: int) -> tuple[int, int, int]:
    xp = max(0, int(total_xp))
    level = 0
    while xp >= _xp_for_next_level(level):
        xp -= _xp_for_next_level(level)
        level += 1
    return level, xp, _xp_for_next_level(level)


def _fmt_xp(value: int) -> str:
    return f"{int(value):,} XP"


def _cut(text: str, limit: int = 1000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _mode_allows(mode: str, source: str) -> bool:
    if mode == "both":
        return True
    return mode == source

def _load_font(size: int, bold: bool = False, extra_bold: bool = False) -> ImageFont.FreeTypeFont:
    base_dir = Path(__file__).resolve().parents[1]
    font_dir = base_dir / "assets" / "fonts"

    # KERISKEDU_R.ttf가 다른 폰트보다 작게 보이므로 regular만 살짝 키움
    regular_size_bonus = 2

    if extra_bold:
        candidates = [
            (font_dir / "KERISKEDU_B.ttf", size),
        ]
    elif bold:
        candidates = [
            (font_dir / "KERISKEDU_Line.ttf", size),
        ]
    else:
        candidates = [
            (font_dir / "KERISKEDU_R.ttf", size + regular_size_bonus),
        ]

    # fallback 폰트들
    candidates.extend(
        [
            (Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"), size),
            (Path("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"), size),
            (Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"), size),
            (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), size),
            (Path("C:/Windows/Fonts/malgun.ttf"), size),
            (Path("C:/Windows/Fonts/malgunbd.ttf"), size),
        ]
    )

    for path, font_size in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), font_size)
            except Exception:
                continue

    return ImageFont.load_default()


def _draw_round_bar(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    height: int,
    value: int,
    max_value: int,
    fill_color: tuple[int, int, int],
    bg_color: tuple[int, int, int] = (43, 46, 51),
    outline_color: tuple[int, int, int] | None = LIGHT_OUTLINE,
    outline_width: int = 2,
    inner_padding: int = 3,
):
    """
    둥근 경험치 바를 그립니다.

    구조:
    1. 바깥 외곽선
    2. 안쪽 배경
    3. 안쪽 채움
    4. 마지막에 외곽선을 한 번 더 그려서 선명하게 유지
    """
    value = max(0, int(value))
    max_value = max(0, int(max_value))
    width = max(1, int(width))
    height = max(1, int(height))

    # 바깥 영역
    outer_box = (x, y, x + width, y + height)
    outer_radius = height // 2

    # 외곽선이 없으면 기존처럼 배경만
    if outline_color is None or outline_width <= 0:
        draw.rounded_rectangle(
            outer_box,
            radius=outer_radius,
            fill=bg_color,
        )
        inner_x = x
        inner_y = y
        inner_w = width
        inner_h = height
    else:
        # 바깥 테두리
        draw.rounded_rectangle(
            outer_box,
            radius=outer_radius,
            fill=outline_color,
        )

        # 내부 트랙
        inner_x = x + outline_width
        inner_y = y + outline_width
        inner_w = width - (outline_width * 2)
        inner_h = height - (outline_width * 2)

        if inner_w <= 0 or inner_h <= 0:
            return

        draw.rounded_rectangle(
            (inner_x, inner_y, inner_x + inner_w, inner_y + inner_h),
            radius=inner_h // 2,
            fill=bg_color,
        )

    if max_value > 0 and value > 0:
        ratio = max(0.0, min(value / max_value, 1.0))

        # 채움 영역은 내부보다 살짝 더 안쪽에 그려서 외곽선과 여백이 보이게 함
        fill_x = inner_x + inner_padding
        fill_y = inner_y + inner_padding
        fill_w_max = inner_w - (inner_padding * 2)
        fill_h = inner_h - (inner_padding * 2)

        if fill_w_max > 0 and fill_h > 0:
            filled_w = int(fill_w_max * ratio)

            # 아주 조금이라도 경험치가 있으면 최소 표시
            if filled_w <= 0:
                filled_w = 1

            draw.rounded_rectangle(
                (fill_x, fill_y, fill_x + filled_w, fill_y + fill_h),
                radius=fill_h // 2,
                fill=fill_color,
            )

    # 마지막 외곽선 재강조
    if outline_color is not None and outline_width > 0:
        draw.rounded_rectangle(
            outer_box,
            radius=outer_radius,
            outline=outline_color,
            width=outline_width,
        )
    
def _draw_shadow_panel(
    draw: ImageDraw.ImageDraw,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    fill: tuple[int, int, int],
    shadow_offset: int = 4,
    shadow_fill: tuple[int, int, int, int] = (0, 0, 0, 90),
):
    # 간단한 그림자 느낌
    draw.rounded_rectangle(
        (x1 + shadow_offset, y1 + shadow_offset, x2 + shadow_offset, y2 + shadow_offset),
        radius=radius,
        fill=shadow_fill,
    )
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=fill)


def _draw_rank_badge(
    draw: ImageDraw.ImageDraw,
    center_x: int,
    center_y: int,
    rank_no: int,
    font_big,
    font_small,
):
    if rank_no == 1:
        fill = (255, 215, 0)
        text_fill = (35, 35, 35)
        label = "1"
    elif rank_no == 2:
        fill = (192, 192, 192)
        text_fill = (35, 35, 35)
        label = "2"
    elif rank_no == 3:
        fill = (205, 127, 50)
        text_fill = (255, 255, 255)
        label = "3"
    else:
        fill = (88, 101, 242)
        text_fill = (255, 255, 255)
        label = str(rank_no)

    r = 24
    draw.ellipse((center_x - r, center_y - r, center_x + r, center_y + r), fill=fill)

    font = font_big if len(label) <= 2 else font_small
    bbox = draw.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        (center_x - tw / 2, center_y - th / 2 - 2),
        label,
        font=font,
        fill=text_fill,
    )


def _draw_label_chip(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font,
    fill_color: tuple[int, int, int],
    text_color: tuple[int, int, int] = (255, 255, 255),
    padding_x: int = 12,
    height: int = 26,
    outline_color: tuple[int, int, int] | None = LIGHT_OUTLINE,
    outline_width: int = 2,
):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    width = tw + padding_x * 2

    radius = height // 2
    chip_box = (x, y, x + width, y + height)

    # 칩 본체
    draw.rounded_rectangle(
        chip_box,
        radius=radius,
        fill=fill_color,
        outline=outline_color,
        width=outline_width if outline_color else 0,
    )

    # 텍스트
    text_x = x + padding_x
    text_y = y + (height - th) / 2 - 2
    draw.text((text_x, text_y), text, font=font, fill=text_color)

    return width


def _draw_separator(
    draw: ImageDraw.ImageDraw,
    x1: int,
    x2: int,
    y: int,
    fill: tuple[int, int, int] = (58, 61, 68),
):
    draw.line((x1, y, x2, y), fill=fill, width=1)
    
class Leveling(commands.Cog):
    """ProBot 스타일의 채팅/음성 XP, 레벨, 랭킹, 보상 역할 시스템."""

    config = app_commands.Group(name="레벨설정", description="레벨/랭킹 시스템 설정")
    rewards = app_commands.Group(name="레벨보상", description="레벨 보상 역할 설정")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._init_db()
        self.voice_xp_loop.start()

    def cog_unload(self):
        self.voice_xp_loop.cancel()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS level_config (
                    guild_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    level_type TEXT NOT NULL DEFAULT 'both',
                    min_text_xp INTEGER NOT NULL DEFAULT 15,
                    max_text_xp INTEGER NOT NULL DEFAULT 25,
                    message_cooldown_seconds INTEGER NOT NULL DEFAULT 60,
                    voice_xp_per_minute INTEGER NOT NULL DEFAULT 10,
                    levelup_channel_id INTEGER,
                    levelup_message TEXT NOT NULL DEFAULT '🎉 {user}님이 **레벨 {level}** 달성!'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS levels (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    text_xp INTEGER NOT NULL DEFAULT 0,
                    voice_xp INTEGER NOT NULL DEFAULT 0,
                    last_message_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS no_xp_channels (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, channel_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS no_xp_roles (
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, role_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS level_rewards (
                    guild_id INTEGER NOT NULL,
                    level INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    remove_lower INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, level, role_id)
                )
                """
            )
            conn.commit()

    def _get_config(self, guild_id: int) -> LevelConfig:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM level_config WHERE guild_id = ?", (guild_id,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO level_config (guild_id) VALUES (?)", (guild_id,))
                conn.commit()
                return LevelConfig()
            return LevelConfig(
                enabled=bool(row["enabled"]),
                level_type=str(row["level_type"]),
                min_text_xp=int(row["min_text_xp"]),
                max_text_xp=int(row["max_text_xp"]),
                message_cooldown_seconds=int(row["message_cooldown_seconds"]),
                voice_xp_per_minute=int(row["voice_xp_per_minute"]),
                levelup_channel_id=int(row["levelup_channel_id"]) if row["levelup_channel_id"] else None,
                levelup_message=str(row["levelup_message"]),
            )

    def _get_xp(self, guild_id: int, user_id: int) -> tuple[int, int]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT text_xp, voice_xp FROM levels WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        if row is None:
            return 0, 0
        return int(row["text_xp"]), int(row["voice_xp"])

    def _get_rank(self, guild_id: int, user_id: int, rank_type: str) -> Optional[int]:
        expr = self._xp_expr(rank_type)
        with self._db() as conn:
            row = conn.execute(
                f"""
                SELECT rank FROM (
                    SELECT user_id, RANK() OVER (ORDER BY {expr} DESC) AS rank
                    FROM levels
                    WHERE guild_id = ? AND {expr} > 0
                ) ranked
                WHERE user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
        return int(row["rank"]) if row else None

    def _xp_expr(self, rank_type: str) -> str:
        if rank_type == "text":
            return "text_xp"
        if rank_type == "voice":
            return "voice_xp"
        return "text_xp + voice_xp"

    def _is_no_xp_channel(self, guild_id: int, channel_id: int) -> bool:
        with self._db() as conn:
            row = conn.execute(
                "SELECT 1 FROM no_xp_channels WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            ).fetchone()
        return row is not None

    def _has_no_xp_role(self, member: discord.Member) -> bool:
        role_ids = [role.id for role in member.roles]
        if not role_ids:
            return False
        placeholders = ",".join("?" for _ in role_ids)
        with self._db() as conn:
            row = conn.execute(
                f"SELECT 1 FROM no_xp_roles WHERE guild_id = ? AND role_id IN ({placeholders}) LIMIT 1",
                [member.guild.id] + role_ids,
            ).fetchone()
        return row is not None

    def _can_gain_xp(self, member: discord.Member, channel: Optional[discord.abc.GuildChannel], source: str) -> bool:
        if member.bot:
            return False
        cfg = self._get_config(member.guild.id)
        if not cfg.enabled:
            return False
        if not _mode_allows(cfg.level_type, source):
            return False
        if channel is not None and self._is_no_xp_channel(member.guild.id, channel.id):
            return False
        if self._has_no_xp_role(member):
            return False
        return True

    def _add_xp(self, guild_id: int, user_id: int, source: str, amount: int, *, update_message_time: bool = False) -> tuple[int, int]:
        amount = max(0, int(amount))
        if amount <= 0:
            text_xp, voice_xp = self._get_xp(guild_id, user_id)
            return text_xp, voice_xp

        col = "text_xp" if source == "text" else "voice_xp"
        now = _now_ts()
        with self._db() as conn:
            old_row = conn.execute(
                "SELECT text_xp, voice_xp FROM levels WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            old_total = 0 if old_row is None else int(old_row["text_xp"]) + int(old_row["voice_xp"])

            conn.execute(
                f"""
                INSERT INTO levels (guild_id, user_id, {col}, last_message_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET
                    {col} = {col} + excluded.{col},
                    last_message_at = CASE
                        WHEN ? THEN excluded.last_message_at
                        ELSE levels.last_message_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (guild_id, user_id, amount, now if update_message_time else 0, now, 1 if update_message_time else 0),
            )
            new_row = conn.execute(
                "SELECT text_xp, voice_xp FROM levels WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            conn.commit()

        new_total = int(new_row["text_xp"]) + int(new_row["voice_xp"])
        return old_total, new_total

    def _message_cooldown_ok(self, guild_id: int, user_id: int, cooldown_seconds: int) -> bool:
        with self._db() as conn:
            row = conn.execute(
                "SELECT last_message_at FROM levels WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        if row is None:
            return True
        return (_now_ts() - float(row["last_message_at"])) >= cooldown_seconds
    
    async def _render_rank_card(
        self,
        member: discord.Member,
        rank_no: int | None,
        level: int,
        total_xp: int,
        text_xp: int,
        voice_xp: int,
        current_xp: int,
        needed_xp: int,
    ) -> io.BytesIO:
        width, height = 920, 520
        card = Image.new("RGBA", (width, height), (24, 26, 27, 255))
        draw = ImageDraw.Draw(card)

        # 색상
        panel = (32, 34, 37)
        panel_soft = (39, 42, 46)
        panel_top = (40, 43, 48)
        accent = (88, 101, 242)
        text_main = (255, 255, 255)
        text_sub = (185, 187, 190)
        green = (87, 242, 135)
        orange = (255, 163, 72)

        # 폰트
        title_font = _load_font(34, bold=True)
        name_font = _load_font(30, bold=True)
        big_font = _load_font(26, bold=True)
        normal_font = _load_font(20)
        small_font = _load_font(17)

        # 배경 패널
        draw.rounded_rectangle((20, 20, width - 20, height - 20), radius=26, fill=panel)

        # 상단 강조 바
        _draw_shadow_panel(draw, 20, 20, width - 20, height - 20, radius=26, fill=panel)
        _draw_shadow_panel(draw, 36, 36, width - 36, 96, radius=22, fill=panel_top)

        draw.text((56, 49), "RANK CARD", font=title_font, fill=text_main)

        # 아바타
        avatar_bytes = await member.display_avatar.replace(size=256).read()
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((140, 140))

        mask = Image.new("L", (140, 140), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((0, 0, 139, 139), fill=255)
        avatar = ImageOps.fit(avatar, (140, 140))
        avatar.putalpha(mask)

        card.paste(avatar, (50, 110), avatar)

        # 이름 / 기본 정보
        draw.text((220, 115), member.display_name, font=name_font, fill=text_main)
        draw.text((220, 158), f"전체 순위  #{rank_no if rank_no else '-'}", font=normal_font, fill=text_sub)
        draw.text((220, 190), f"레벨  Lv.{level}", font=normal_font, fill=text_sub)
        draw.text((220, 222), f"전체 경험치  {_fmt_xp(total_xp)}", font=normal_font, fill=text_sub)

        # 진행도 박스
        _draw_shadow_panel(draw, 50, 285, 870, 355, radius=18, fill=panel_soft)
        draw.text((70, 300), "다음 레벨 진행도", font=normal_font, fill=text_main)

        _draw_round_bar(
            draw,
            x=250,
            y=302,
            width=560,
            height=22,
            value=current_xp,
            max_value=max(needed_xp, 1),
            fill_color=accent,
        )

        draw.text(
            (250, 329),
            f"{_fmt_xp(current_xp)} / {_fmt_xp(needed_xp)}",
            font=small_font,
            fill=text_sub,
        )

        # 채팅 경험치
        _draw_shadow_panel(draw, 50, 380, 870, 438, radius=18, fill=panel_soft)
        _draw_label_chip(
            draw,
            70,
            392,
            "채팅",
            small_font,
            green,
            text_color=(20, 20, 20),
            outline_color=LIGHT_OUTLINE,
            outline_width=2,
        )

        _draw_round_bar(
            draw,
            x=250,
            y=397,
            width=560,
            height=18,
            value=text_xp,
            max_value=max(total_xp, 1),
            fill_color=green,
        )

        draw.text((250, 418), _fmt_xp(text_xp), font=small_font, fill=text_sub)

        # 음성 경험치
        _draw_shadow_panel(draw, 50, 448, 870, 506, radius=18, fill=panel_soft)
        _draw_label_chip(
            draw,
            70,
            460,
            "음성",
            small_font,
            orange,
            text_color=(20, 20, 20),
            outline_color=LIGHT_OUTLINE,
            outline_width=2,
        )

        _draw_round_bar(
            draw,
            x=250,
            y=465,
            width=560,
            height=18,
            value=voice_xp,
            max_value=max(total_xp, 1),
            fill_color=orange,
        )

        draw.text((250, 486), _fmt_xp(voice_xp), font=small_font, fill=text_sub)

        # 저장
        buffer = io.BytesIO()
        card.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer
    
    async def _handle_level_up(self, guild: discord.Guild, member: discord.Member, old_total_xp: int, new_total_xp: int) -> None:
        old_level = _level_from_xp(old_total_xp)
        new_level = _level_from_xp(new_total_xp)
        if new_level <= old_level:
            return

        await self._apply_level_rewards(member, new_level)
        await self._send_levelup_message(guild, member, new_level)

    async def _apply_level_rewards(self, member: discord.Member, new_level: int) -> None:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT level, role_id, remove_lower
                FROM level_rewards
                WHERE guild_id = ? AND level <= ?
                ORDER BY level ASC
                """,
                (member.guild.id, new_level),
            ).fetchall()

        if not rows:
            return

        roles_to_add: list[discord.Role] = []
        lower_roles_to_remove: set[discord.Role] = set()

        for row in rows:
            role = member.guild.get_role(int(row["role_id"]))
            if role is None:
                continue
            if int(row["level"]) == new_level and role not in member.roles:
                roles_to_add.append(role)
            if int(row["level"]) < new_level and bool(row["remove_lower"]):
                if role in member.roles:
                    lower_roles_to_remove.add(role)

        try:
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="Level reward")
            if lower_roles_to_remove:
                await member.remove_roles(*lower_roles_to_remove, reason="Remove lower level reward")
        except discord.Forbidden:
            print(f"[leveling] missing permission or role hierarchy blocked: guild={member.guild.id}, user={member.id}")
        except discord.HTTPException as exc:
            print(f"[leveling] reward role update failed: {type(exc).__name__}: {exc}")

    async def _send_levelup_message(self, guild: discord.Guild, member: discord.Member, level: int) -> None:
        cfg = self._get_config(guild.id)
        channel: Optional[discord.abc.Messageable] = None

        if cfg.levelup_channel_id:
            maybe_channel = guild.get_channel(cfg.levelup_channel_id)
            if isinstance(maybe_channel, discord.abc.Messageable):
                channel = maybe_channel

        if channel is None:
            return

        text_xp, voice_xp = self._get_xp(guild.id, member.id)
        total_xp = text_xp + voice_xp
        rank = self._get_rank(guild.id, member.id, "total")

        message = cfg.levelup_message.format(
            user=member.mention,
            username=member.display_name,
            server=guild.name,
            level=level,
            xp=total_xp,
            rank=rank or "-",
        )
        try:
            await channel.send(_cut(message, 1900))
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[leveling] levelup send failed: {type(exc).__name__}: {exc}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return
        if not isinstance(message.author, discord.Member):
            return
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.abc.GuildChannel):
            return
        if not self._can_gain_xp(message.author, message.channel, "text"):
            return

        cfg = self._get_config(message.guild.id)
        if not self._message_cooldown_ok(message.guild.id, message.author.id, cfg.message_cooldown_seconds):
            return

        amount = random.randint(min(cfg.min_text_xp, cfg.max_text_xp), max(cfg.min_text_xp, cfg.max_text_xp))
        old_total, new_total = self._add_xp(
            message.guild.id,
            message.author.id,
            "text",
            amount,
            update_message_time=True,
        )
        await self._handle_level_up(message.guild, message.author, old_total, new_total)

    @tasks.loop(minutes=1)
    async def voice_xp_loop(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            cfg = self._get_config(guild.id)
            if not cfg.enabled or not _mode_allows(cfg.level_type, "voice") or cfg.voice_xp_per_minute <= 0:
                continue

            for channel in guild.voice_channels:
                if self._is_no_xp_channel(guild.id, channel.id):
                    continue
                for member in channel.members:
                    if not self._can_gain_xp(member, channel, "voice"):
                        continue
                    old_total, new_total = self._add_xp(guild.id, member.id, "voice", cfg.voice_xp_per_minute)
                    await self._handle_level_up(guild, member, old_total, new_total)

    @voice_xp_loop.before_loop
    async def before_voice_xp_loop(self):
        await self.bot.wait_until_ready()
        
    @app_commands.command(name="랭킹", description="서버 전체 경험치 랭킹을 이미지로 확인합니다.")
    @app_commands.describe(page="페이지 번호")
    @app_commands.rename(page="페이지")
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        page: int = 1,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.",
                ephemeral=True,
            )

        await interaction.response.defer()

        page = max(1, min(int(page), 50))
        per_page = 10
        offset = (page - 1) * per_page

        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT user_id, text_xp, voice_xp, total_xp, rank_no
                FROM (
                    SELECT
                        user_id,
                        text_xp,
                        voice_xp,
                        text_xp + voice_xp AS total_xp,
                        RANK() OVER (ORDER BY text_xp + voice_xp DESC) AS rank_no
                    FROM levels
                    WHERE guild_id = ?
                    AND text_xp + voice_xp > 0
                ) ranked
                ORDER BY rank_no ASC
                LIMIT ? OFFSET ?
                """,
                (interaction.guild.id, per_page, offset),
            ).fetchall()

        if not rows:
            return await interaction.followup.send("아직 랭킹 기록이 없습니다.")

        image_buffer = await self._render_leaderboard_card(
            guild=interaction.guild,
            rows=rows,
            page=page,
            per_page=per_page,
        )

        file = discord.File(image_buffer, filename="leaderboard.png")

        embed = discord.Embed(color=discord.Color.gold())
        embed.set_image(url="attachment://leaderboard.png")

        await interaction.followup.send(embed=embed, file=file)
        
    @app_commands.command(name="랭크", description="내 랭크 또는 특정 유저의 랭크를 확인합니다.")
    @app_commands.describe(user="조회할 유저")
    @app_commands.rename(user="유저")
    async def rank(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.",
                ephemeral=True,
            )

        target = user or interaction.user

        if not isinstance(target, discord.Member):
            return await interaction.response.send_message(
                "서버 멤버만 조회할 수 있습니다.",
                ephemeral=True,
            )

        await interaction.response.defer()

        text_xp, voice_xp = self._get_xp(interaction.guild.id, target.id)
        total_xp = text_xp + voice_xp

        level, current, needed = _progress_in_level(total_xp)
        rank_no = self._get_rank(interaction.guild.id, target.id, "total")

        image_buffer = await self._render_rank_card(
            member=target,
            rank_no=rank_no,
            level=level,
            total_xp=total_xp,
            text_xp=text_xp,
            voice_xp=voice_xp,
            current_xp=current,
            needed_xp=needed,
        )

        file = discord.File(image_buffer, filename="rank_card.png")

        embed = discord.Embed(color=discord.Color.gold())
        embed.set_image(url="attachment://rank_card.png")

        await interaction.followup.send(embed=embed, file=file)
        
    async def _render_leaderboard_card(
        self,
        guild: discord.Guild,
        rows,
        page: int,
        per_page: int,
    ) -> io.BytesIO:
        count = len(rows)

        width = 1180
        header_h = 150
        row_h = 118
        bottom_h = 50
        height = header_h + (row_h * count) + bottom_h

        image = Image.new("RGBA", (width, height), (24, 26, 27, 255))
        draw = ImageDraw.Draw(image)

        # 팔레트
        bg = (24, 26, 27)
        main_panel = (32, 34, 37)
        header_panel = (36, 39, 44)
        row_panel = (39, 42, 46)
        row_panel_alt = (42, 45, 49)
        text_main = (255, 255, 255)
        text_sub = (185, 187, 190)
        accent = (88, 101, 242)
        green = (87, 242, 135)
        orange = (255, 163, 72)
        border = (58, 61, 68)

        # 폰트
        title_font = _load_font(36, bold=True)
        subtitle_font = _load_font(18)
        name_font = _load_font(24, bold=True)
        normal_font = _load_font(18)
        small_font = _load_font(15)
        badge_font = _load_font(22, bold=True)
        badge_small_font = _load_font(18, bold=True)

        # 바깥 패널
        _draw_shadow_panel(draw, 18, 18, width - 18, height - 18, radius=28, fill=main_panel)

        # 헤더
        _draw_shadow_panel(draw, 34, 34, width - 34, 118, radius=24, fill=header_panel)

        draw.text((58, 48), f"{guild.name} 전체 랭킹", font=title_font, fill=text_main)
        draw.text(
            (58, 90),
            f"페이지 {page} · 전체 경험치 기준 순위",
            font=subtitle_font,
            fill=text_sub,
        )

        # 헤더 우측 서버 아이콘
        if guild.icon:
            try:
                icon_bytes = await guild.icon.replace(size=128).read()
                icon = Image.open(io.BytesIO(icon_bytes)).convert("RGBA").resize((58, 58))

                mask = Image.new("L", (58, 58), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.ellipse((0, 0, 57, 57), fill=255)
                icon = ImageOps.fit(icon, (58, 58))
                icon.putalpha(mask)

                image.paste(icon, (width - 105, 46), icon)
            except Exception:
                pass

        max_total_xp = max(max(int(row["total_xp"]), 1) for row in rows)

        for i, row in enumerate(rows):
            y = header_h + (i * row_h)

            user_id = int(row["user_id"])
            text_xp = int(row["text_xp"])
            voice_xp = int(row["voice_xp"])
            total_xp = int(row["total_xp"])
            rank_no = int(row["rank_no"])

            member = guild.get_member(user_id)
            display_name = member.display_name if member else f"알 수 없는 유저 ({user_id})"
            level = _level_from_xp(total_xp)

            panel_fill = row_panel if i % 2 == 0 else row_panel_alt
            _draw_shadow_panel(draw, 40, y + 8, width - 40, y + row_h - 10, radius=20, fill=panel_fill)

            # 순위 배지
            _draw_rank_badge(
                draw,
                center_x=76,
                center_y=y + 58,
                rank_no=rank_no,
                font_big=badge_font,
                font_small=badge_small_font,
            )

            # 아바타
            avatar_x = 125
            avatar_y = y + 27
            avatar_size = 62

            if member:
                try:
                    avatar_bytes = await member.display_avatar.replace(size=128).read()
                    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((avatar_size, avatar_size))

                    mask = Image.new("L", (avatar_size, avatar_size), 0)
                    mask_draw = ImageDraw.Draw(mask)
                    mask_draw.ellipse((0, 0, avatar_size - 1, avatar_size - 1), fill=255)
                    avatar = ImageOps.fit(avatar, (avatar_size, avatar_size))
                    avatar.putalpha(mask)

                    image.paste(avatar, (avatar_x, avatar_y), avatar)
                except Exception:
                    draw.ellipse(
                        (avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size),
                        fill=(70, 73, 80),
                    )
            else:
                draw.ellipse(
                    (avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size),
                    fill=(70, 73, 80),
                )

            # 이름 / 기본 정보
            info_x = 210
            draw.text((info_x, y + 22), display_name, font=name_font, fill=text_main)

            _draw_label_chip(draw, info_x, y + 58, f"Lv.{level}", small_font, accent)
            _draw_label_chip(draw, info_x + 88, y + 58, f"전체 XP {_fmt_xp(total_xp)}", small_font, (64, 68, 75))

            # 오른쪽 경험치 영역
            stat_x = 560
            bar_x = stat_x + 78
            bar_w = 410
            bar_h = 16

            _draw_label_chip(
                draw,
                stat_x,
                y + 20,
                "채팅",
                small_font,
                green,
                text_color=(20, 20, 20),
                outline_color=LIGHT_OUTLINE,
                outline_width=2,
            )
            _draw_round_bar(
                draw,
                x=bar_x,
                y=y + 24,
                width=bar_w,
                height=bar_h,
                value=text_xp,
                max_value=max_total_xp,
                fill_color=green,
            )
            draw.text((bar_x, y + 44), _fmt_xp(text_xp), font=small_font, fill=text_sub)

            _draw_label_chip(
                draw,
                stat_x,
                y + 66,
                "음성",
                small_font,
                orange,
                text_color=(20, 20, 20),
                outline_color=LIGHT_OUTLINE,
                outline_width=2,
            )
            _draw_round_bar(
                draw,
                x=bar_x,
                y=y + 70,
                width=bar_w,
                height=bar_h,
                value=voice_xp,
                max_value=max_total_xp,
                fill_color=orange,
            )
            draw.text((bar_x, y + 90), _fmt_xp(voice_xp), font=small_font, fill=text_sub)

        # 하단 설명
        footer_text = f"페이지당 {per_page}명 표시 · 채팅/음성 바 길이는 현재 페이지 최고 전체 XP 기준"

        footer_line_y = height - 52
        footer_text_y = height - 40

        _draw_separator(draw, 46, width - 46, footer_line_y, fill=border)

        draw.text(
            (52, footer_text_y),
            footer_text,
            font=small_font,
            fill=text_sub,
        )

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer

    @config.command(name="보기", description="현재 레벨 시스템 설정을 확인합니다.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_show(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)

        cfg = self._get_config(interaction.guild.id)
        channel = interaction.guild.get_channel(cfg.levelup_channel_id) if cfg.levelup_channel_id else None

        with self._db() as conn:
            no_channels = conn.execute(
                "SELECT channel_id FROM no_xp_channels WHERE guild_id = ?",
                (interaction.guild.id,),
            ).fetchall()
            no_roles = conn.execute(
                "SELECT role_id FROM no_xp_roles WHERE guild_id = ?",
                (interaction.guild.id,),
            ).fetchall()

        no_channel_text = []
        for row in no_channels:
            ch = interaction.guild.get_channel(int(row["channel_id"]))
            no_channel_text.append(ch.mention if hasattr(ch, "mention") else f"삭제된 채널 `{row['channel_id']}`")

        no_role_text = []
        for row in no_roles:
            role = interaction.guild.get_role(int(row["role_id"]))
            no_role_text.append(role.mention if role else f"삭제된 역할 `{row['role_id']}`")

        embed = discord.Embed(title="레벨 시스템 설정", color=discord.Color.blurple())
        embed.add_field(name="활성화", value="ON" if cfg.enabled else "OFF", inline=True)
        embed.add_field(name="집계 방식", value=cfg.level_type, inline=True)
        embed.add_field(name="채팅 경험치", value=f"{cfg.min_text_xp}~{cfg.max_text_xp} XP / 쿨다운 {cfg.message_cooldown_seconds}초", inline=False)
        embed.add_field(name="음성 경험치", value=f"분당 {cfg.voice_xp_per_minute} XP", inline=False)
        embed.add_field(name="레벨업 채널", value=channel.mention if channel else "설정 안 됨", inline=False)
        embed.add_field(name="경험치 제외 채널", value="\n".join(no_channel_text) if no_channel_text else "없음", inline=False)
        embed.add_field(name="경험치 제외 역할", value="\n".join(no_role_text) if no_role_text else "없음", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config.command(name="사용", description="레벨 시스템을 켜거나 끕니다.")
    @app_commands.describe(enabled="켜려면 True, 끄려면 False")
    @app_commands.rename(enabled="활성화")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_enabled(self, interaction: discord.Interaction, enabled: bool):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        with self._db() as conn:
            conn.execute("INSERT OR IGNORE INTO level_config (guild_id) VALUES (?)", (interaction.guild.id,))
            conn.execute("UPDATE level_config SET enabled = ? WHERE guild_id = ?", (1 if enabled else 0, interaction.guild.id))
            conn.commit()
        await interaction.response.send_message(f"레벨 시스템을 {'켰습니다' if enabled else '껐습니다'}.", ephemeral=True)

    @config.command(name="방식", description="경험치 집계 방식을 설정합니다.")
    @app_commands.describe(level_type="채팅만/음성만/채팅+음성")
    @app_commands.rename(level_type="방식")
    @app_commands.choices(level_type=CONFIG_LEVEL_TYPE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_type(self, interaction: discord.Interaction, level_type: str):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        with self._db() as conn:
            conn.execute("INSERT OR IGNORE INTO level_config (guild_id) VALUES (?)", (interaction.guild.id,))
            conn.execute("UPDATE level_config SET level_type = ? WHERE guild_id = ?", (level_type, interaction.guild.id))
            conn.commit()
        await interaction.response.send_message(f"경험치 집계 방식을 `{LEVEL_MODE_NAMES.get(level_type, level_type)}`로 설정했습니다.", ephemeral=True)

    @config.command(name="채팅경험치", description="채팅 경험치와 쿨다운을 설정합니다.")
    @app_commands.describe(min_xp="메시지당 최소 경험치", max_xp="메시지당 최대 경험치", cooldown_seconds="경험치 획득 쿨다운 초")
    @app_commands.rename(min_xp="최소경험치", max_xp="최대경험치", cooldown_seconds="쿨다운초")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_text_xp(self, interaction: discord.Interaction, min_xp: int, max_xp: int, cooldown_seconds: int):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        min_xp = max(0, min(int(min_xp), 1000))
        max_xp = max(min_xp, min(int(max_xp), 1000))
        cooldown_seconds = max(0, min(int(cooldown_seconds), 86400))
        with self._db() as conn:
            conn.execute("INSERT OR IGNORE INTO level_config (guild_id) VALUES (?)", (interaction.guild.id,))
            conn.execute(
                "UPDATE level_config SET min_text_xp = ?, max_text_xp = ?, message_cooldown_seconds = ? WHERE guild_id = ?",
                (min_xp, max_xp, cooldown_seconds, interaction.guild.id),
            )
            conn.commit()
        await interaction.response.send_message(
            f"채팅 경험치를 `{min_xp}~{max_xp}`, 쿨다운을 `{cooldown_seconds}초`로 설정했습니다.",
            ephemeral=True,
        )

    @config.command(name="음성경험치", description="음성방 경험치를 설정합니다.")
    @app_commands.describe(xp_per_minute="음성방 1분당 경험치")
    @app_commands.rename(xp_per_minute="분당경험치")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_voice_xp(self, interaction: discord.Interaction, xp_per_minute: int):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        xp_per_minute = max(0, min(int(xp_per_minute), 10000))
        with self._db() as conn:
            conn.execute("INSERT OR IGNORE INTO level_config (guild_id) VALUES (?)", (interaction.guild.id,))
            conn.execute("UPDATE level_config SET voice_xp_per_minute = ? WHERE guild_id = ?", (xp_per_minute, interaction.guild.id))
            conn.commit()
        await interaction.response.send_message(f"음성 경험치를 분당 `{xp_per_minute} XP`로 설정했습니다.", ephemeral=True)

    @config.command(name="레벨업채널", description="레벨업 메시지를 보낼 채널을 설정합니다.")
    @app_commands.rename(channel="채널")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_levelup_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        with self._db() as conn:
            conn.execute("INSERT OR IGNORE INTO level_config (guild_id) VALUES (?)", (interaction.guild.id,))
            conn.execute("UPDATE level_config SET levelup_channel_id = ? WHERE guild_id = ?", (channel.id, interaction.guild.id))
            conn.commit()
        await interaction.response.send_message(f"레벨업 메시지 채널을 {channel.mention}로 설정했습니다.", ephemeral=True)

    @config.command(name="레벨업메시지", description="레벨업 메시지 문구를 설정합니다.")
    @app_commands.describe(message="사용 가능 변수: {user}, {username}, {server}, {level}, {xp}, {rank}")
    @app_commands.rename(message="메시지")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_levelup_message(self, interaction: discord.Interaction, message: str):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        message = _cut(message, 300)
        with self._db() as conn:
            conn.execute("INSERT OR IGNORE INTO level_config (guild_id) VALUES (?)", (interaction.guild.id,))
            conn.execute("UPDATE level_config SET levelup_message = ? WHERE guild_id = ?", (message, interaction.guild.id))
            conn.commit()
        await interaction.response.send_message("레벨업 메시지를 변경했습니다.", ephemeral=True)

    @config.command(name="제외채널추가", description="경험치를 얻지 못하는 채널을 추가합니다.")
    @app_commands.rename(channel="채널")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_no_xp_channel_add(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        with self._db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO no_xp_channels (guild_id, channel_id) VALUES (?, ?)",
                (interaction.guild.id, channel.id),
            )
            conn.commit()
        await interaction.response.send_message(f"{channel.mention} 채널을 경험치 제외 채널에 추가했습니다.", ephemeral=True)

    @config.command(name="제외채널제거", description="경험치 제외 채널을 제거합니다.")
    @app_commands.rename(channel="채널")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_no_xp_channel_remove(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        with self._db() as conn:
            conn.execute(
                "DELETE FROM no_xp_channels WHERE guild_id = ? AND channel_id = ?",
                (interaction.guild.id, channel.id),
            )
            conn.commit()
        await interaction.response.send_message(f"{channel.mention} 채널을 경험치 제외 채널에서 제거했습니다.", ephemeral=True)

    @config.command(name="제외역할추가", description="경험치를 얻지 못하는 역할을 추가합니다.")
    @app_commands.rename(role="역할")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_no_xp_role_add(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        with self._db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO no_xp_roles (guild_id, role_id) VALUES (?, ?)",
                (interaction.guild.id, role.id),
            )
            conn.commit()
        await interaction.response.send_message(f"{role.mention} 역할을 경험치 제외 역할에 추가했습니다.", ephemeral=True)

    @config.command(name="제외역할제거", description="경험치 제외 역할을 제거합니다.")
    @app_commands.rename(role="역할")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_no_xp_role_remove(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        with self._db() as conn:
            conn.execute(
                "DELETE FROM no_xp_roles WHERE guild_id = ? AND role_id = ?",
                (interaction.guild.id, role.id),
            )
            conn.commit()
        await interaction.response.send_message(f"{role.mention} 역할을 경험치 제외 역할에서 제거했습니다.", ephemeral=True)

    @rewards.command(name="추가", description="특정 레벨 달성 시 지급할 역할을 추가합니다.")
    @app_commands.describe(level="필요 레벨", role="지급할 역할", remove_lower="상위 보상 지급 시 낮은 보상 역할 제거 여부")
    @app_commands.rename(level="레벨", role="역할", remove_lower="하위제거")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reward_add(self, interaction: discord.Interaction, level: int, role: discord.Role, remove_lower: bool = False):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        level = max(1, min(int(level), 1000))
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO level_rewards (guild_id, level, role_id, remove_lower)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, level, role_id)
                DO UPDATE SET remove_lower = excluded.remove_lower
                """,
                (interaction.guild.id, level, role.id, 1 if remove_lower else 0),
            )
            conn.commit()
        await interaction.response.send_message(f"Lv.{level} 보상으로 {role.mention} 역할을 등록했습니다.", ephemeral=True)

    @rewards.command(name="제거", description="레벨 보상 역할을 제거합니다.")
    @app_commands.rename(level="레벨", role="역할")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reward_remove(self, interaction: discord.Interaction, level: int, role: discord.Role):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        with self._db() as conn:
            conn.execute(
                "DELETE FROM level_rewards WHERE guild_id = ? AND level = ? AND role_id = ?",
                (interaction.guild.id, level, role.id),
            )
            conn.commit()
        await interaction.response.send_message(f"Lv.{level} 보상에서 {role.mention} 역할을 제거했습니다.", ephemeral=True)

    @rewards.command(name="보기", description="레벨 보상 역할 목록을 확인합니다.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def reward_show(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        with self._db() as conn:
            rows = conn.execute(
                "SELECT level, role_id, remove_lower FROM level_rewards WHERE guild_id = ? ORDER BY level ASC",
                (interaction.guild.id,),
            ).fetchall()
        if not rows:
            return await interaction.response.send_message("등록된 레벨 보상이 없습니다.", ephemeral=True)

        lines = []
        for row in rows:
            role = interaction.guild.get_role(int(row["role_id"]))
            role_text = role.mention if role else f"삭제된 역할 `{row['role_id']}`"
            remove_text = " / 하위 제거" if bool(row["remove_lower"]) else ""
            lines.append(f"- Lv.{row['level']}: {role_text}{remove_text}")
        await interaction.response.send_message("레벨 보상 목록:\n" + "\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Leveling(bot))
