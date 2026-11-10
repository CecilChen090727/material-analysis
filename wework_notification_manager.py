# @author   : linhao.chen01
# @time     : 2026/11/4 15:31

"""Send application file messages using credentials from the project .env.

    manager = WeWorkNotificationManager()
    result = manager.send_file(r"excel\\prices.xlsx", touser="member_userid")

Alternatively, call upload_file() to obtain a media_id (valid for three days),
then pass it to send_file_message(). Recipients must be in the app's scope.
"""

import json
import mimetypes
from pathlib import Path
from time import monotonic
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class WeWorkSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent / ".env",
        env_file_encoding="utf-8",
        env_prefix="WEWORK_",
        extra="ignore",
    )

    corpid: str = "wx4dfadd2396eca6da"
    agent_id: int = Field(default=1000144, gt=0)
    secret: SecretStr = Field(min_length=1)


class WeWorkAPIError(RuntimeError):
    def __init__(self, operation: str, result: dict):
        self.errcode = result.get("errcode")
        self.result = result
        super().__init__(
            f"WeCom {operation} failed ({self.errcode}): "
            f"{result.get('errmsg', 'unknown API error')}"
        )


class WeWorkNotificationManager:
    BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"
    MAX_FILE_SIZE = 20 * 1024 * 1024

    def __init__(self, settings: WeWorkSettings | None = None, *, timeout: float = 30):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.settings = settings if settings is not None else WeWorkSettings()
        self.timeout = timeout
        self._access_token = ""
        self._token_expires_at = 0.0

    def _request(
            self, operation: str, params: dict, data: bytes | None = None,
            content_type: str | None = None,
    ) -> dict:
        headers = {"Content-Type": content_type} if content_type else {}
        request = Request(
            f"{self.BASE_URL}/{operation}?{urlencode(params)}",
            data=data,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read())
        except (URLError, OSError):
            # urllib errors can include the URL containing the secret or token.
            raise RuntimeError(f"WeCom {operation} HTTP request failed") from None
        except (ValueError, UnicodeError):
            raise RuntimeError(f"WeCom {operation} returned invalid JSON") from None
        if not isinstance(result, dict):
            raise RuntimeError(f"WeCom {operation} returned an invalid response")
        if result.get("errcode") != 0:
            raise WeWorkAPIError(operation, result)
        return result

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        if (
                not force_refresh
                and self._access_token
                and monotonic() < self._token_expires_at
        ):
            return self._access_token
        started_at = monotonic()
        result = self._request("gettoken", {
            "corpid": self.settings.corpid,
            "corpsecret": self.settings.secret.get_secret_value(),
        })
        token = result.get("access_token")
        expires_in = result.get("expires_in")
        if (
                not isinstance(token, str) or not token
                or not isinstance(expires_in, (int, float)) or expires_in <= 0
        ):
            raise RuntimeError("WeCom gettoken returned invalid token data")
        self._access_token = token
        self._token_expires_at = started_at + max(0, expires_in - 60)
        return token

    def _authenticated_request(
            self, operation: str, data: bytes, content_type: str,
            **params,
    ) -> dict:
        for attempt in range(2):
            token = self.get_access_token(force_refresh=attempt > 0)
            try:
                return self._request(
                    operation, {**params, "access_token": token},
                    data, content_type,
                )
            except WeWorkAPIError as error:
                if attempt or error.errcode not in (40001, 40014, 42001):
                    raise

    def upload_file(self, file_path: str | Path) -> str:
        """Upload a temporary file and return its media_id (valid for 3 days)."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        if not 5 < path.stat().st_size <= self.MAX_FILE_SIZE:
            raise ValueError("File size must be greater than 5 bytes and at most 20 MB")
        with path.open("rb") as source:
            content = source.read(self.MAX_FILE_SIZE + 1)
        if not 5 < len(content) <= self.MAX_FILE_SIZE:
            raise ValueError("File size must be greater than 5 bytes and at most 20 MB")

        boundary = uuid4().hex
        filename = (
            path.name.replace("\r", "").replace("\n", "")
            .replace("\\", "\\\\").replace('"', '\\"')
        )
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = (
                   f"--{boundary}\r\n"
                   f'Content-Disposition: form-data; name="media"; filename="{filename}"; '
                   f"filelength={len(content)}\r\n"
                   f"Content-Type: {content_type}\r\n\r\n"
               ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        result = self._authenticated_request(
            "media/upload", body, f"multipart/form-data; boundary={boundary}",
            type="file",
        )
        media_id = result.get("media_id")
        if not isinstance(media_id, str) or not media_id:
            raise RuntimeError("WeCom media/upload returned no media_id")
        return media_id

    @staticmethod
    def _validate_recipients(touser: str, toparty: str, totag: str) -> None:
        if not any(value.strip() for value in (touser, toparty, totag)):
            raise ValueError("At least one of touser, toparty or totag is required")

    def send_file_message(
            self, media_id: str, *, touser: str = "", toparty: str = "",
            totag: str = "", safe: bool = False,
    ) -> dict:
        """Send an existing media_id; preserve partial-recipient errors in the result.

        Recipients are pipe-separated IDs, e.g. touser="user1|user2".
        touser="@all" explicitly sends to all members in the app's scope.
        """
        self._validate_recipients(touser, toparty, totag)
        if not media_id.strip():
            raise ValueError("media_id must not be empty")
        payload = {
            "touser": touser,
            "toparty": toparty,
            "totag": totag,
            "msgtype": "file",
            "agentid": self.settings.agent_id,
            "file": {"media_id": media_id},
            "safe": int(safe),
        }
        return self._authenticated_request(
            "message/send", json.dumps(payload).encode("utf-8"), "application/json",
        )

    def send_file(
            self, file_path: str | Path, *, touser: str = "", toparty: str = "",
            totag: str = "", safe: bool = False,
    ) -> dict:
        """Upload a local file and send it without needing to manage media_id."""
        self._validate_recipients(touser, toparty, totag)
        media_id = self.upload_file(file_path)
        return self.send_file_message(
            media_id, touser=touser, toparty=toparty, totag=totag, safe=safe,
        )
