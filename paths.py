from pathlib import Path
import json


def app_dir() -> Path:
    path = Path.home() / "Library" / "Application Support" / "BridgeCommander"
    path.mkdir(parents=True, exist_ok=True)
    return path


def known_hosts_path() -> Path:
    path = app_dir() / "known_hosts"
    path.touch(exist_ok=True)
    return path


def load_config() -> dict:
    path = app_dir() / "settings.json"
    if not path.exists():
        return {"sites": [], "bookmarks": [], "show_hidden": False,
                "local_path": str(Path.home())}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sites": [], "bookmarks": [], "show_hidden": False,
                "local_path": str(Path.home())}


def save_config(config: dict):
    path = app_dir() / "settings.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
