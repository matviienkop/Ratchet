"""Card audit, retry semantics and delivery address isolation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_dev import history, notifications, review_summary
from ai_dev.store import Card, CardError


class CardHistory(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {"AI_DEV_HOME": temp.name, "HERMES_KANBAN_RUN_ID": "45",
                                      "HERMES_KANBAN_TASK": "", "HERMES_SESSION_ID": "sess"})
        env.start()
        self.addCleanup(env.stop)
        self.card = Card("project", "t_1")

    def test_handoff_and_tool_history(self):
        handoff = argparse.Namespace(finished="tests", result="password=superSECRET", stage="review",
                                    actor="reviewer", inputs="receipt-1", reason="verified")
        for _ in range(2):
            history.handoff(self.card, handoff)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: history.observe(self.card, "terminal", {"command": "password=unsafe"},
                                                    result="private output", status="ok", tool_call_id=str(i)),
                          range(8)))
        result, _ = history.history(self.card, argparse.Namespace(details=True, native=False, limit=8))
        self.assertEqual(result["total_events"], 1)
        self.assertEqual(result["events"][0]["run"], "45")
        self.assertEqual(len(result["tools"]), 8)
        for private in ("superSECRET", "unsafe", "private output"):
            self.assertNotIn(private, repr(result))

    def test_shell_without_route_adopts_single_confirmed_native_subscription(self):
        row = '[{"platform":"telegram","chat_id":"123","thread_id":"5"}]'
        with patch.dict(os.environ, {"HERMES_SESSION_PLATFORM":"", "HERMES_SESSION_CHAT_ID":""}), \
                patch("ai_dev.history.subprocess.run", return_value=subprocess.CompletedProcess([], 0, row)) as call:
            result, _ = history.bind(self.card)
            self.assertEqual(result["conversation"]["thread"], "5")
            self.assertIsNone(result["conversation"]["session"])
            self.assertEqual(call.call_count, 1)
            self.assertIn("notify-list", call.call_args.args[0])

    def test_full_review_list_is_split_in_order_with_stable_ids_and_no_attachments(self):
        with self.card.locked() as state:
            keys = [f"key-{i:02}" for i in range(45)]
            for key in keys:
                state["ledger"][key] = {"key":key, "title":"MEDIA:/private file " + "ж"*90, "status":"note", "severity":"low"}
            rnd = {"n":2,"tree":"abc","panel":{},"reports":{"peer":{"findings":keys}}}
            review_summary.queue(state, rnd, "accept")
            review_summary.queue(state, rnd, "accept")
        events = self.card.load()["log"]
        texts = [notifications.message(self.card, e) for e in events if e["event"] == "review.summary-message"]
        self.assertGreater(len(texts), 1)
        self.assertEqual(len([e for e in events if e["event"] == "review.round-summary"]), 1)
        self.assertTrue(all(len(t.encode("utf-16-le"))//2 <= 4096 and "MEDIA:" not in t for t in texts))
        self.assertTrue(all("".join(texts).count("(" + k + ")") == 1 for k in keys))

    def test_retry_delivery_preserves_order_and_confirms_once(self):
        from unittest.mock import Mock
        with self.card.locked() as state:
            state["conversation"] = {"platform":"slack", "chat":"C012345678", "thread":"123.456"}
        args = argparse.Namespace(finished="impl", result="MEDIA:/private/file.pdf", stage="review",
                                  actor="reviewer", inputs="tree", reason="verified")
        history.handoff(self.card, args)
        history.handoff(self.card, args)
        args.stage = "delivery"
        history.handoff(self.card, args)
        sender = Mock(side_effect=[subprocess.CompletedProcess([], 0, '{"skipped":true}'),
                                   subprocess.CompletedProcess([], 0, '{"success":true,"message_id":"m1"}'),
                                   subprocess.CompletedProcess([], 0, '{"success":true}')])
        with patch("ai_dev.notifications.subprocess.run", sender), patch("ai_dev.notifications.time.time", return_value=1):
            self.assertEqual(notifications.deliver(self.card), "retry")
            self.assertEqual(notifications.deliver(self.card), "idle")
        with patch("ai_dev.notifications.subprocess.run", sender), patch("ai_dev.notifications.time.time", return_value=3601):
            self.assertEqual(notifications.deliver(self.card), "sent")
            self.assertEqual(notifications.deliver(self.card), "sent")
            self.assertEqual(notifications.deliver(self.card), "idle")
        first, retry, second = sender.call_args_list
        self.assertEqual(first, retry)
        self.assertNotIn("MEDIA:", first.kwargs["input"])
        self.assertIn("slack:C012345678:123.456", first.args[0])
        self.assertIn("h1", first.kwargs["input"])
        self.assertIn("h2", second.kwargs["input"])
        receipts = [e["delivery"] for e in self.card.load()["log"]]
        self.assertEqual([r["attempts"] for r in receipts], [2, 1])
        self.assertEqual(receipts[0]["message_id"], "m1")
        self.assertEqual(notifications.target({"platform":"telegram","chat":"-123","thread":"5"}),"telegram:-123:5")
        with self.assertRaises(CardError):
            notifications.target({"platform":"slack","chat":"C012345678","thread":""})

    def test_binding_preserves_original_thread_and_retries_failure(self):
        good = subprocess.CompletedProcess([], 0)
        with patch.dict(os.environ, {"HERMES_SESSION_PLATFORM": "slack", "HERMES_SESSION_CHAT_ID": "C1",
                                     "HERMES_SESSION_THREAD_ID": "1.2"}):
            with patch("ai_dev.history.subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaises(CardError):
                    history.bind(self.card)
            self.assertNotIn("conversation", self.card.load())
            with patch("ai_dev.history.subprocess.run", return_value=good) as call:
                history.bind(self.card)
                history.bind(self.card)
                self.assertIn("--thread-id", call.call_args.args[0])
                os.environ["HERMES_SESSION_THREAD_ID"] = "2.3"
                with self.assertRaises(CardError):
                    history.bind(self.card)
                self.assertEqual(call.call_count, 2)
        self.assertEqual(self.card.load()["conversation"]["thread"], "1.2")
        self.assertEqual(len(self.card.load()["log"]), 1)
