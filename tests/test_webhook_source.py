"""Inbound webhooks from systems that send their own signature.

GitHub (HMAC over the raw body) and GitLab (shared token in a header) are the
two shapes covered; there is no vendor-specific code, only config.
"""

import hashlib
import hmac
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from hub.adapters.generic_source import GenericSource
from hub.config import Config
from hub.engine import Engine
from hub.server import make_server
from hub.store import Store

SECRET = "s3cr3t-for-tests"


def sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


class SourceUnitTest(unittest.TestCase):
    def test_token_auth(self):
        src = GenericSource("gitlab", {"auth": {"type": "token", "header": "X-Gitlab-Token", "secret": SECRET}})
        self.assertTrue(src.verify(b"", {"x-gitlab-token": SECRET}))
        self.assertFalse(src.verify(b"", {"x-gitlab-token": "nope"}))
        self.assertFalse(src.verify(b"", {}))

    def test_hmac_auth_and_prefix(self):
        raw = b'{"a":1}'
        src = GenericSource("gh", {"auth": {"type": "hmac_sha256", "header": "X-Hub-Signature-256", "secret": SECRET}})
        self.assertTrue(src.verify(raw, {"X-Hub-Signature-256": sign(raw)}))
        self.assertTrue(src.verify(raw, {"X-Hub-Signature-256": sign(raw)[7:]}))
        self.assertFalse(src.verify(b'{"a":2}', {"X-Hub-Signature-256": sign(raw)}))

    def test_map_accepts_a_fallback_path_list(self):
        src = GenericSource("tapd", {"map": {"title": ["title", "old_title"]}})
        self.assertEqual(src.parse({"old_title": "旧标题"}, {})["title"], "旧标题")
        self.assertEqual(src.parse({"title": "新标题"}, {})["title"], "新标题")

    def test_accept_filters_events(self):
        src = GenericSource(
            "tapd",
            {"accept": ["bug::create"], "map": {"kind": "event", "external_id": "id"}},
        )
        self.assertIsNone(src.parse({"event": "bug::update", "id": "1"}, {}))
        self.assertIsNotNone(src.parse({"event": "bug::create", "id": "1"}, {}))

    def test_query_auth(self):
        src = GenericSource("tapd", {"auth": {"type": "query", "param": "token", "secret": SECRET}})
        self.assertTrue(src.verify(b"", {}, {"token": [SECRET]}))
        self.assertFalse(src.verify(b"", {}, {"token": ["nope"]}))
        self.assertFalse(src.verify(b"", {}, {}))

    def test_missing_secret_fails_closed(self):
        src = GenericSource("gh", {"auth": {"type": "hmac_sha256", "header": "X-Hub-Signature-256", "secret_env": "HUB_NO_SUCH_SECRET"}})
        os.environ.pop("HUB_NO_SUCH_SECRET", None)
        self.assertFalse(src.verify(b"x", {"X-Hub-Signature-256": sign(b"x")}))

    def test_headers_win_over_body(self):
        src = GenericSource(
            "gh",
            {
                "map": {"kind": "action", "external_id": "id", "title": "title"},
                "headers": {"kind": "X-GitHub-Event", "external_id": "X-GitHub-Delivery"},
            },
        )
        parsed = src.parse(
            {"action": "opened", "id": 7, "title": "fix"},
            {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1"},
        )
        self.assertEqual(parsed["kind"], "pull_request")
        self.assertEqual(parsed["external_id"], "d-1")
        self.assertEqual(parsed["title"], "fix")


class HookEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.old_secret = os.environ.get("HUB_TEST_SECRET")
        os.environ["HUB_TEST_SECRET"] = SECRET
        cfg = Config()
        cfg.bind, cfg.port, cfg.api_token = "127.0.0.1", 0, "api-token"
        cfg.agents = {}
        cfg.notifiers = {}
        cfg.projects = {"p1": {}}
        cfg.sources = {
            "github": {
                "type": "generic",
                "map": {"title": "pull_request.title"},
                "headers": {"kind": "X-GitHub-Event", "external_id": "X-GitHub-Delivery"},
                "auth": {
                    "type": "hmac_sha256",
                    "header": "X-Hub-Signature-256",
                    "secret_env": "HUB_TEST_SECRET",
                },
            },
            "open": {
                "type": "generic",
                "map": {"external_id": "id", "title": "title"},
                "project": "p1",
            },
            "tapd": {
                "type": "generic",
                "map": {"kind": "event", "external_id": "id", "title": "title"},
                "auth": {"type": "query", "param": "token", "secret_env": "HUB_TEST_SECRET"},
            },
        }
        self.store = Store(Path(self.tmp.name) / "hub.db")
        self.engine = Engine(self.store, cfg, notifiers={})
        self.httpd = make_server(self.store, self.engine, cfg)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        if self.old_secret is None:
            os.environ.pop("HUB_TEST_SECRET", None)
        else:
            os.environ["HUB_TEST_SECRET"] = self.old_secret
        self.tmp.cleanup()

    def post(self, path, payload, headers=None):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_github_delivery_needs_no_shared_token(self):
        payload = {"action": "opened", "pull_request": {"title": "fix the bug"}}
        raw = json.dumps(payload).encode()
        code, out = self.post(
            "/hooks/github",
            payload,
            {"X-Hub-Signature-256": sign(raw), "X-GitHub-Event": "pull_request",
             "X-GitHub-Delivery": "d-1"},
        )
        self.assertEqual(code, 200, out)
        self.assertTrue(out["created"])
        self.assertEqual(out["task"]["kind"], "pull_request")
        self.assertEqual(out["task"]["external_id"], "d-1")
        self.assertEqual(out["task"]["title"], "fix the bug")

    def test_redelivery_dedupes_on_delivery_id(self):
        payload = {"pull_request": {"title": "t"}}
        raw = json.dumps(payload).encode()
        headers = {"X-Hub-Signature-256": sign(raw), "X-GitHub-Event": "pull_request",
                   "X-GitHub-Delivery": "d-2"}
        first = self.post("/hooks/github", payload, headers)[1]
        second = self.post("/hooks/github", payload, headers)[1]
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["task_id"], second["task_id"])

    def test_bad_signature_is_rejected(self):
        code, out = self.post(
            "/hooks/github",
            {"pull_request": {"title": "t"}},
            {"X-Hub-Signature-256": "deadbeef", "X-GitHub-Delivery": "d-3"},
        )
        self.assertEqual(code, 401, out)

    def test_missing_signature_is_rejected(self):
        code, _ = self.post("/hooks/github", {"pull_request": {"title": "t"}})
        self.assertEqual(code, 401)

    def test_unprotected_source_still_needs_the_api_token(self):
        code, _ = self.post("/hooks/open", {"id": "x", "title": "t"})
        self.assertEqual(code, 401)
        code, out = self.post(
            "/hooks/open", {"id": "x", "title": "t"}, {"X-Hub-Token": "api-token"}
        )
        self.assertEqual(code, 200, out)
        self.assertTrue(out["created"])
        # The source declares its project; the body has no project field.
        self.assertEqual(out["task"]["project"], "p1")

    def test_query_token_source(self):
        code, out = self.post("/hooks/tapd?token=wrong", {"id": "1", "title": "t"})
        self.assertEqual(code, 401, out)
        code, out = self.post(f"/hooks/tapd?token={SECRET}", {"id": "1", "title": "t"})
        self.assertEqual(code, 200, out)
        self.assertTrue(out["created"])

    def test_unknown_source_needs_the_api_token(self):
        self.assertEqual(self.post("/hooks/nope", {})[0], 401)
        code, _ = self.post("/hooks/nope", {}, {"X-Hub-Token": "api-token"})
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()