import json
from pathlib import Path
import unittest
from unittest.mock import patch

from main import main, push_file_message


class PushFileMessageTests(unittest.TestCase):
    def setUp(self):
        manager_patch = patch("main.WeWorkNotificationManager")
        self.manager_class = manager_patch.start()
        self.addCleanup(manager_patch.stop)
        self.manager = self.manager_class.return_value

    def test_file_path_broadcasts_without_recipient_config_and_returns_api_result(self):
        result = {"errcode": 0, "msgid": "message", "invaliduser": "user2"}
        self.manager.send_file.return_value = result
        path = Path("excel") / "prices.xlsx"

        self.assertEqual(push_file_message(path), result)

        self.manager_class.assert_called_once_with()
        self.manager.send_file.assert_called_once_with(path, touser="@all")

    def test_send_failure_is_not_reported_as_success(self):
        self.manager.send_file.side_effect = FileNotFoundError("missing.xlsx")
        with self.assertRaises(FileNotFoundError):
            push_file_message("missing.xlsx")

    def test_cli_passes_path_and_prints_api_response(self):
        result = {"errcode": 0, "msgid": "message"}
        with (
            patch("main.push_file_message", return_value=result) as push,
            patch("builtins.print") as output,
        ):
            main([r"excel\prices.xlsx"])
        push.assert_called_once_with(Path(r"excel\prices.xlsx"))
        self.assertEqual(json.loads(output.call_args.args[0]), result)


if __name__ == "__main__":
    unittest.main()
