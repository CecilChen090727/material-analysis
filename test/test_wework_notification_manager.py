from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from wework_notification_manager import (
    WeWorkAPIError,
    WeWorkNotificationManager,
    WeWorkSettings,
)


class WeWorkNotificationManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = WeWorkNotificationManager(WeWorkSettings(
            _env_file=None, corpid="test-corp", agent_id=123,
            secret="test-secret",
        ))
        self.urlopen = patch("wework_notification_manager.urlopen").start()
        self.addCleanup(patch.stopall)

    def responses(self, *results):
        responses = []
        for result in results:
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(result).encode()
            responses.append(response)
        self.urlopen.side_effect = responses

    def token_response(self, token="test-token"):
        return {"errcode": 0, "access_token": token, "expires_in": 7200}

    def test_upload_and_send_local_file(self):
        self.responses(
            self.token_response(),
            {"errcode": 0, "media_id": "uploaded-media"},
            {"errcode": 0, "msgid": "message-id", "invaliduser": "missing-user"},
        )
        content = b"example report\r\n\x00\xff"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.xlsx"
            path.write_bytes(content)
            result = self.manager.send_file(path, touser="user1|missing-user", safe=True)

        token_request, upload_request, send_request = [
            call.args[0] for call in self.urlopen.call_args_list
        ]
        self.assertEqual(parse_qs(urlsplit(token_request.full_url).query), {
            "corpid": ["test-corp"], "corpsecret": ["test-secret"],
        })
        self.assertEqual(parse_qs(urlsplit(upload_request.full_url).query), {
            "type": ["file"], "access_token": ["test-token"],
        })
        multipart = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {upload_request.get_header('Content-type')}\r\n\r\n".encode()
            + upload_request.data
        )
        part = list(multipart.iter_parts())[0]
        self.assertEqual(part.get_param("name", header="content-disposition"), "media")
        self.assertEqual(part.get_filename(), "report.xlsx")
        self.assertEqual(part.get_payload(decode=True), content)
        self.assertEqual(json.loads(send_request.data), {
            "touser": "user1|missing-user", "toparty": "", "totag": "",
            "msgtype": "file", "agentid": 123,
            "file": {"media_id": "uploaded-media"}, "safe": 1,
        })
        self.assertEqual(result["invaliduser"], "missing-user")
        self.assertEqual(result["msgid"], "message-id")
        self.assertEqual(self.urlopen.call_count, 3)
        self.assertTrue(all(call.kwargs["timeout"] == 30
                            for call in self.urlopen.call_args_list))

    def test_cached_token_refreshes_before_expiry(self):
        self.responses(self.token_response("first"), self.token_response("second"))
        with patch("wework_notification_manager.monotonic",
                   side_effect=[100, 200, 7240, 7240]):
            self.assertEqual(self.manager.get_access_token(), "first")
            self.assertEqual(self.manager.get_access_token(), "first")
            self.assertEqual(self.manager.get_access_token(), "second")
        self.assertEqual(self.urlopen.call_count, 2)

    def test_invalid_token_refreshes_and_retries_message_once(self):
        self.responses(
            self.token_response("old"), {"errcode": 42001, "errmsg": "expired"},
            self.token_response("new"), {"errcode": 0, "msgid": "sent"},
        )
        result = self.manager.send_file_message("existing-media", toparty="1|2")
        self.assertEqual(result["msgid"], "sent")
        sends = [call.args[0] for call in self.urlopen.call_args_list
                 if "/message/send?" in call.args[0].full_url]
        self.assertEqual(sends[0].data, sends[1].data)
        self.assertEqual(parse_qs(urlsplit(sends[1].full_url).query),
                         {"access_token": ["new"]})

    def test_persistent_token_error_does_not_retry_forever(self):
        self.responses(
            self.token_response(), {"errcode": 40014},
            self.token_response(), {"errcode": 40014},
        )
        with self.assertRaises(WeWorkAPIError):
            self.manager.send_file_message("media", touser="user1")
        self.assertEqual(self.urlopen.call_count, 4)

    def test_upload_retry_keeps_file_body(self):
        self.responses(
            self.token_response(), {"errcode": 40014},
            self.token_response("new"), {"errcode": 0, "media_id": "media"},
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.txt"
            path.write_bytes(b"report")
            self.assertEqual(self.manager.upload_file(path), "media")
        self.assertEqual(self.urlopen.call_args_list[1].args[0].data,
                         self.urlopen.call_args_list[3].args[0].data)

    def test_invalid_recipients_and_media_fail_without_network_or_upload(self):
        with patch.object(self.manager, "upload_file") as upload:
            with self.assertRaises(ValueError):
                self.manager.send_file("missing.xlsx", touser=" ")
            upload.assert_not_called()
        with self.assertRaises(ValueError):
            self.manager.send_file_message(" ", totag="3")
        self.urlopen.assert_not_called()

    def test_missing_small_and_oversized_files_fail_before_network(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.txt"
            with self.assertRaises(FileNotFoundError):
                self.manager.upload_file(path)
            for size in (0, 5, self.manager.MAX_FILE_SIZE + 1):
                with self.subTest(size=size):
                    with path.open("wb") as file:
                        file.truncate(size)
                    with self.assertRaises(ValueError):
                        self.manager.upload_file(path)
        self.urlopen.assert_not_called()

    def test_api_error_is_reported_without_retry(self):
        self.responses(self.token_response(),
                       {"errcode": 81013, "errmsg": "invalid recipients"})
        with self.assertRaises(WeWorkAPIError) as raised:
            self.manager.send_file_message("media", touser="user1")
        self.assertEqual(raised.exception.errcode, 81013)
        self.assertEqual(self.urlopen.call_count, 2)

    def test_http_failure_does_not_expose_secret_in_exception(self):
        self.urlopen.side_effect = URLError("URL contains test-secret")
        with self.assertRaises(RuntimeError) as raised:
            self.manager.get_access_token()
        self.assertNotIn("test-secret", str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertEqual(self.urlopen.call_count, 1)

    def test_missing_upload_media_id_is_rejected(self):
        self.responses(self.token_response(), {"errcode": 0})
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.txt"
            path.write_bytes(b"report")
            with self.assertRaisesRegex(RuntimeError, "media_id"):
                self.manager.upload_file(path)

    def test_settings_read_wework_environment_variables(self):
        with patch.dict("os.environ", {
            "WEWORK_CORPID": "env-corp", "WEWORK_AGENT_ID": "456",
            "WEWORK_SECRET": "env-secret",
        }):
            settings = WeWorkSettings(_env_file=None)
        self.assertEqual(settings.corpid, "env-corp")
        self.assertEqual(settings.agent_id, 456)
        self.assertEqual(settings.secret.get_secret_value(), "env-secret")
        self.assertNotIn("env-secret", repr(settings))


if __name__ == "__main__":
    unittest.main()
