"""Feishu inbound gateway: parsing + opt-in behaviour, no SDK required.

The gateway module imports ``lark_oapi`` lazily, so these tests run against the
standard library alone — which is the point: the core stays dependency-free.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from hub.adapters.feishu_notifier import FeishuNotifier, read_binding, write_binding
from hub.feishu_gateway import FeishuGateway, parse_card_action, parse_message


def message_event(
    text="修个登录缺陷",
    open_id="ou_user",
    chat_id="oc_chat",
    message_id="om_1",
    sender_type="user",
    message_type="text",
    content=None,
):
    if content is None:
        import json

        content = json.dumps({"text": text}, ensure_ascii=False)
    return SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type=sender_type,
                sender_id=SimpleNamespace(open_id=open_id, user_id="uid_1"),
            ),
            message=SimpleNamespace(
                message_id=message_id,
                chat_id=chat_id,
                chat_type="p2p",
                message_type=message_type,
                content=content,
            ),
        )
    )


def card_event(value, open_id="ou_user"):
    return SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=open_id),
            action=SimpleNamespace(value=value, tag="button"),
        )
    )


class ParseMessageTest(unittest.TestCase):
    def test_extracts_text_and_identity(self):
        msg = parse_message(message_event())
        self.assertEqual(msg["text"], "修个登录缺陷")
        self.assertEqual(msg["open_id"], "ou_user")
        self.assertEqual(msg["chat_id"], "oc_chat")
        self.assertEqual(msg["message_id"], "om_1")

    def test_ignores_messages_from_other_bots(self):
        self.assertIsNone(parse_message(message_event(sender_type="app")))

    def test_non_json_content_is_kept_verbatim(self):
        msg = parse_message(message_event(content="plain text"))
        self.assertEqual(msg["text"], "plain text")

    def test_missing_event_returns_none(self):
        self.assertIsNone(parse_message(object()))


class ParseCardActionTest(unittest.TestCase):
    def test_reads_token_and_index(self):
        action = parse_card_action(card_event({"token": "tok-1", "index": 1}))
        self.assertEqual(action, {"token": "tok-1", "index": 1, "open_id": "ou_user"})

    def test_unknown_button_returns_none(self):
        self.assertIsNone(parse_card_action(card_event({"something": "else"})))


class BindingTest(unittest.TestCase):
    def test_binding_round_trip(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "feishu_binding.json"
            write_binding(path, open_id="ou_x", chat_id="oc_y")
            self.assertEqual(read_binding(path)["open_id"], "ou_x")
            self.assertEqual(read_binding(path)["chat_id"], "oc_y")

    def test_notifier_falls_back_to_binding(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            notifier = FeishuNotifier({"app_id": "a", "app_secret": "s"}, tmp + "/outbox")
            self.assertEqual(notifier.resolve_target(), ("chat_id", ""))
            write_binding(notifier.binding_path, open_id="ou_bound")
            self.assertEqual(notifier.resolve_target(), ("open_id", "ou_bound"))
            self.assertTrue(notifier.has_creds)

    def test_explicit_receive_id_beats_binding(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            notifier = FeishuNotifier(
                {
                    "app_id": "a",
                    "app_secret": "s",
                    "receive_id_type": "chat_id",
                    "receive_id": "oc_config",
                },
                tmp + "/outbox",
            )
            write_binding(notifier.binding_path, open_id="ou_bound")
            self.assertEqual(notifier.resolve_target(), ("chat_id", "oc_config"))


class GatewayOptInTest(unittest.TestCase):
    def _gateway(self, entry):
        from tempfile import TemporaryDirectory

        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        return FeishuGateway(None, None, entry, stage="intake", outbox_dir=self._tmp.name + "/outbox")

    def test_start_without_sdk_returns_false_and_warns(self):
        gateway = self._gateway({"app_id": "a", "app_secret": "s", "inbound": True})
        with mock.patch("builtins.__import__", side_effect=ImportError("no sdk")):
            self.assertFalse(gateway.start())

    def test_start_without_credentials_returns_false(self):
        gateway = self._gateway({"inbound": True})
        self.assertFalse(gateway.start())

    def test_allowed_open_ids_restricts_access(self):
        gateway = self._gateway({"app_id": "a", "app_secret": "s", "allowed_open_ids": ["ou_ok"]})
        self.assertTrue(gateway._allowed("ou_ok"))
        self.assertFalse(gateway._allowed("ou_other"))

    def test_empty_allow_list_allows_everyone(self):
        gateway = self._gateway({"app_id": "a", "app_secret": "s"})
        self.assertTrue(gateway._allowed("ou_anyone"))


if __name__ == "__main__":
    unittest.main()