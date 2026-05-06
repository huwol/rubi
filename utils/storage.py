from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", BASE_DIR / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

ROLEBTN_STORAGE = DATA_DIR / "rolebuttons_config.json"
GUILD_CFG_STORAGE = DATA_DIR / "guild_config.json"


def load_json(path: Path, default: Any) -> Any:
    """깨진 JSON이나 없는 파일 때문에 봇 전체가 죽지 않게 안전하게 읽습니다."""
    path = Path(path)
    try:
        if not path.exists():
            return default
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return default
        return json.loads(text)
    except Exception as exc:
        print(f"[storage] JSON load failed: {path} ({type(exc).__name__}: {exc})")
        return default


def save_json(path: Path, data: Any) -> None:
    """쓰기 중 강제 종료되어도 파일이 반쯤 깨지지 않도록 원자적 저장을 사용합니다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except OSError:
            pass


# ---- Role Buttons ----
def load_rolebtn_configs() -> List[Dict[str, Any]]:
    return load_json(ROLEBTN_STORAGE, [])


def save_rolebtn_configs(cfgs: List[Dict[str, Any]]) -> None:
    save_json(ROLEBTN_STORAGE, cfgs)


def append_rolebtn_config(cfg: Dict[str, Any]) -> None:
    data = load_rolebtn_configs()
    data.append(cfg)
    save_rolebtn_configs(data)


# ---- Guild Config ----
def load_guild_cfg() -> Dict[str, Dict[str, Any]]:
    return load_json(GUILD_CFG_STORAGE, {})


def save_guild_cfg(cfg: Dict[str, Dict[str, Any]]) -> None:
    save_json(GUILD_CFG_STORAGE, cfg)


def update_guild_cfg(guild_id: int, updates: Dict[str, Any]) -> Dict[str, Any]:
    allcfg = load_guild_cfg()
    gid = str(guild_id)
    cur = allcfg.get(gid, {})
    cur.update(updates)
    allcfg[gid] = cur
    save_guild_cfg(allcfg)
    return cur


def get_guild_cfg(guild_id: int) -> Dict[str, Any]:
    return load_guild_cfg().get(str(guild_id), {})
