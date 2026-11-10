import json
from pathlib import Path

from wework_notification_manager import WeWorkNotificationManager


def push_file_message(file_path: str | Path) -> dict:
    """Upload a local file and send it to all members in the app's scope."""
    manager = WeWorkNotificationManager()
    return manager.send_file(file_path, touser="@all")


def main(file_path: str) -> None:
    result = push_file_message(file_path)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main(file_path="C:/gds-dev-code/material-analysis/html/20261109_原材料价格信息.html")
