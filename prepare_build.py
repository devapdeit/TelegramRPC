from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "build_config.json"
OUTPUT = ROOT / "embedded_config.py"


def main() -> None:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    client_id = str(data.get("discord_client_id", "")).strip()
    large_image = str(data.get("large_image", "telegram_music")).strip() or "telegram_music"
    large_text = str(data.get("large_text", "Музыка из Telegram")).strip() or "Музыка из Telegram"

    if not client_id.isdigit() or not (15 <= len(client_id) <= 25):
        raise SystemExit(
            "ERROR: Open build_config.json and replace PUT_YOUR_APPLICATION_ID_HERE "
            "with the numeric Discord Application ID."
        )

    output = (
        "# Generated automatically by prepare_build.py\n"
        f"EMBEDDED_DISCORD_CLIENT_ID = {client_id!r}\n"
        f"EMBEDDED_LARGE_IMAGE = {large_image!r}\n"
        f"EMBEDDED_LARGE_TEXT = {large_text!r}\n"
    )
    OUTPUT.write_text(output, encoding="utf-8")
    print("Embedded build configuration prepared.")


if __name__ == "__main__":
    main()
