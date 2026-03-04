"""슬랙 봇 설정 — 환경변수 로드."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class SlackConfig:
    bot_token: str    # SLACK_BOT_TOKEN=xoxb-...
    app_token: str    # SLACK_APP_TOKEN=xapp-... (Socket Mode)
    channel_id: str   # SLACK_CHANNEL_ID=C0123456789 (정규 요청 채널)
    client_name: str = "wishket"
    published_db: str = "data/wishket_published.json"
    output_dir: str = "output"
    # Google Drive
    google_creds_json: str = ""       # GOOGLE_SERVICE_ACCOUNT_JSON (파일 경로)
    google_drive_folder_id: str = ""  # GOOGLE_DRIVE_FOLDER_ID

    @classmethod
    def from_env(cls) -> "SlackConfig":
        load_dotenv()
        return cls(
            bot_token=os.environ["SLACK_BOT_TOKEN"],
            app_token=os.environ["SLACK_APP_TOKEN"],
            channel_id=os.environ["SLACK_CHANNEL_ID"],
            client_name=os.environ.get("SLACK_CLIENT_NAME", "wishket"),
            published_db=os.environ.get("SLACK_PUBLISHED_DB", "data/wishket_published.json"),
            output_dir=os.environ.get("SLACK_OUTPUT_DIR", "output"),
            google_creds_json=os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""),
            google_drive_folder_id=os.environ.get("GOOGLE_DRIVE_FOLDER_ID", ""),
        )
