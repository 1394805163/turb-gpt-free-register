import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from core.email_provider import parse_email_sources
from core.icloud_mail_pool import ICloudMailboxPool


def otp_mail(address: str, code: str, *, received: datetime | None = None) -> bytes:
    message = EmailMessage()
    message["From"] = "OpenAI <noreply@tm.openai.com>"
    message["To"] = address
    message["Date"] = received or datetime.now(timezone.utc)
    message["Message-ID"] = f"<{address}-{code}@fixture.test>"
    message["Subject"] = f"Your ChatGPT verification code is {code}"
    message.set_content(f"Verification code: {code}")
    return message.as_bytes()


class ScriptedIMAP:
    """只替代外部 IMAP 边界；被测 UID 发现和邮件解析保持真实。"""

    def __init__(
        self,
        snapshots: list[list[bytes]],
        messages: dict[bytes, bytes],
        uidvalidity: bytes | list[bytes] = b"1",
        fetch_sequences: dict[bytes, list[bytes]] | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.fetch_sequences = fetch_sequences or {}
        self.fetch_counts: dict[bytes, int] = {}
        self.refresh_count = 0
        self.uid_calls: list[tuple[str, tuple]] = []
        self.noop_calls = 0
        self.select_calls = 0
        self.logged_out = False

    def uid(self, command, *args):
        self.uid_calls.append((str(command).lower(), args))
        if str(command).lower() == "search":
            index = min(self.refresh_count, len(self.snapshots) - 1)
            return "OK", [b" ".join(self.snapshots[index])]
        if str(command).lower() == "fetch":
            uid = args[0]
            sequence = self.fetch_sequences.get(uid)
            if sequence:
                index = self.fetch_counts.get(uid, 0)
                self.fetch_counts[uid] = index + 1
                raw = sequence[min(index, len(sequence) - 1)]
            else:
                raw = self.messages.get(uid, b"")
            return "OK", [(b"BODY[]", raw)] if raw else []
        raise AssertionError(f"unexpected uid command: {command} {args}")

    def noop(self):
        self.noop_calls += 1
        self.refresh_count += 1
        return "OK", [b""]

    def select(self, _mailbox, readonly=True):
        self.select_calls += 1
        self.refresh_count += 1
        return "OK", [b""]

    def response(self, name):
        if str(name).upper() == "UIDVALIDITY":
            if isinstance(self.uidvalidity, list):
                index = min(self.refresh_count, len(self.uidvalidity) - 1)
                return "UIDVALIDITY", [self.uidvalidity[index]]
            return "UIDVALIDITY", [self.uidvalidity]
        return None, []

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]


class ICloudMailboxPoolTests(unittest.TestCase):
    def pool(self, **overrides) -> ICloudMailboxPool:
        config = {
            "message_limit": 20,
            "initial_scan_limit": 20,
            "wait_timeout": 0.05,
            "wait_interval": 0,
            "reselect_interval": 30,
            "reconnect_interval": 30,
            "clock_skew_seconds": 30,
        }
        config.update(overrides)
        return ICloudMailboxPool(config, Path("unused.json"))

    @staticmethod
    def mailbox(address: str) -> dict:
        return {
            "address": address,
            "_code_not_before": datetime.now(timezone.utc) - timedelta(seconds=1),
            "_seen": set(),
        }

    def test_uid_polling_discovers_new_mail_after_refresh_without_header_search(self):
        """抓住回归：恢复成 HEADER 搜索或不刷新旧会话会再次漏掉已送达 OTP。"""
        target = "alias@icloud.com"
        imap = ScriptedIMAP(
            snapshots=[[b"10"], [b"10", b"11"]],
            messages={b"11": otp_mail(target, "123456")},
        )
        pool = self.pool()

        with patch.object(pool, "_connect_imap", return_value=imap), patch(
            "core.icloud_mail_pool.time.sleep", return_value=None
        ):
            code = pool.wait_for_code(self.mailbox(target))

        self.assertEqual(code, "123456")
        self.assertGreaterEqual(imap.noop_calls, 1)
        self.assertTrue(any(call[1] == (None, "ALL") for call in imap.uid_calls if call[0] == "search"))
        self.assertFalse(any("HEADER" in tuple(map(str, call[1])) for call in imap.uid_calls))

    def test_stale_connection_is_replaced_and_new_connection_finds_otp(self):
        """抓住回归：连接视图永久陈旧时必须在超时前重连，而不是一直复用。"""
        target = "alias@icloud.com"
        stale = ScriptedIMAP(snapshots=[[b"10"]], messages={})
        fresh = ScriptedIMAP(
            snapshots=[[b"10", b"11"]],
            messages={b"11": otp_mail(target, "234567")},
        )
        pool = self.pool(reconnect_interval=0)

        with patch.object(pool, "_connect_imap", side_effect=[stale, fresh]), patch(
            "core.icloud_mail_pool.time.sleep", return_value=None
        ):
            code = pool.wait_for_code(self.mailbox(target))

        self.assertEqual(code, "234567")
        self.assertTrue(stale.logged_out)

    def test_new_uid_from_other_alias_cannot_cross_deliver_code(self):
        """抓住回归：共享主收件箱时不能把另一隐藏邮箱的 OTP 返回给当前任务。"""
        target = "target@icloud.com"
        imap = ScriptedIMAP(
            snapshots=[[b"20", b"21"]],
            messages={
                b"20": otp_mail("other@icloud.com", "345678"),
                b"21": otp_mail(target, "456789"),
            },
        )
        pool = self.pool()

        with patch.object(pool, "_connect_imap", return_value=imap):
            code = pool.wait_for_code(self.mailbox(target))

        self.assertEqual(code, "456789")

    def test_recipient_matching_is_exact_not_header_substring(self):
        """目标地址只是另一收件人的子串时，绝不能读取其 OTP。"""
        target = "target@icloud.com"
        imap = ScriptedIMAP(
            snapshots=[[b"22"]],
            messages={b"22": otp_mail("x-target@icloud.com", "456780")},
        )
        pool = self.pool()

        with patch.object(pool, "_connect_imap", return_value=imap), patch(
            "core.icloud_mail_pool.time.sleep", return_value=None
        ):
            code = pool.wait_for_code(self.mailbox(target))

        self.assertIsNone(code)

    def test_uidvalidity_change_rebuilds_uid_baseline_without_reusing_old_messages(self):
        """抓住回归：邮箱 UID 空间重建后，新 UID 可能小于旧基线，必须重置 UID 状态。"""
        target = "alias@icloud.com"
        imap = ScriptedIMAP(
            snapshots=[[b"100"], [b"100"], [b"1"]],
            messages={b"1": otp_mail(target, "567890")},
            uidvalidity=[b"7", b"7", b"8"],
        )
        pool = self.pool()

        with patch.object(pool, "_connect_imap", return_value=imap), patch(
            "core.icloud_mail_pool.time.sleep", return_value=None
        ):
            code = pool.wait_for_code(self.mailbox(target))

        self.assertEqual(code, "567890")

    def test_transient_empty_fetch_does_not_permanently_skip_new_uid(self):
        """抓住回归：新 UID 第一次 FETCH 为空时，下一轮必须重试该 UID。"""
        target = "alias@icloud.com"
        raw = otp_mail(target, "678901")
        imap = ScriptedIMAP(
            snapshots=[[b"12"]],
            messages={},
            fetch_sequences={b"12": [b"", raw]},
        )
        pool = self.pool()

        with patch.object(pool, "_connect_imap", return_value=imap), patch(
            "core.icloud_mail_pool.time.sleep", return_value=None
        ):
            code = pool.wait_for_code(self.mailbox(target))

        self.assertEqual(code, "678901")
        self.assertEqual(imap.fetch_counts[b"12"], 2)

    def test_imap_connection_timeout_is_bounded_by_remaining_otp_budget(self):
        target = "alias@icloud.com"
        imap = ScriptedIMAP(
            snapshots=[[b"13"]],
            messages={b"13": otp_mail(target, "679012")},
        )
        pool = self.pool(wait_timeout=0.05)

        with patch.object(pool, "_connect_imap", return_value=imap) as connect:
            code = pool.wait_for_code(self.mailbox(target))

        self.assertEqual(code, "679012")
        timeout = connect.call_args.kwargs["request_timeout"]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 0.051)

    def test_connect_login_and_select_each_rebind_the_same_deadline(self):
        class FakeClock:
            def __init__(self):
                self.now = 300.0

            def monotonic(self):
                return self.now

            def advance(self, seconds=0.01):
                self.now += seconds

        clock = FakeClock()
        constructor_timeouts: list[float] = []
        socket_timeouts: list[float] = []

        class FakeSocket:
            timeout = 30.0

            def settimeout(self, timeout):
                self.timeout = timeout
                socket_timeouts.append(timeout)

        class HandshakeIMAP:
            def __init__(self):
                self.sock = FakeSocket()

            def login(self, _username, _password):
                clock.advance(min(0.01, self.sock.timeout))
                return "OK", []

            def select(self, _mailbox, readonly=True):
                clock.advance(min(0.01, self.sock.timeout))
                return "OK", []

            def logout(self):
                return "BYE", []

        def construct(_host, _port, *, timeout):
            constructor_timeouts.append(timeout)
            clock.advance(min(0.01, timeout))
            return HandshakeIMAP()

        pool = self.pool(
            imap_username="main@icloud.com",
            imap_password="fixture-password",
            request_timeout=30,
        )
        with patch("core.icloud_mail_pool.imaplib.IMAP4_SSL", side_effect=construct), patch(
            "core.icloud_mail_pool.time.monotonic", side_effect=clock.monotonic
        ):
            imap = pool._connect_imap(deadline=300.05)

        self.assertIsInstance(imap, HandshakeIMAP)
        self.assertEqual(len(constructor_timeouts), 1)
        self.assertEqual(len(socket_timeouts), 2)
        all_timeouts = constructor_timeouts + socket_timeouts
        self.assertTrue(all(a > b for a, b in zip(all_timeouts, all_timeouts[1:])))
        self.assertTrue(all(0 < timeout <= 0.051 for timeout in all_timeouts))

    def test_logout_uses_remaining_budget_and_expired_deadline_aborts_socket(self):
        class FakeClock:
            def __init__(self, now):
                self.now = now

            def monotonic(self):
                return self.now

        clock = FakeClock(400.024)

        class FakeSocket:
            def __init__(self):
                self.timeout = 30.0
                self.closed = False

            def settimeout(self, timeout):
                self.timeout = timeout

            def close(self):
                self.closed = True

        class ClosingIMAP:
            def __init__(self):
                self.sock = FakeSocket()
                self.logout_calls = 0

            def logout(self):
                self.logout_calls += 1
                clock.now += min(0.01, self.sock.timeout)
                return "BYE", []

        pool = self.pool()
        before_deadline = ClosingIMAP()
        with patch("core.icloud_mail_pool.time.monotonic", side_effect=clock.monotonic):
            pool._close_imap(before_deadline, deadline=400.025)

        self.assertEqual(before_deadline.logout_calls, 1)
        self.assertLessEqual(clock.now, 400.025 + 1e-9)

        expired = ClosingIMAP()
        clock.now = 400.025
        with patch("core.icloud_mail_pool.time.monotonic", side_effect=clock.monotonic):
            pool._close_imap(expired, deadline=400.025)

        self.assertEqual(expired.logout_calls, 0)
        self.assertTrue(expired.sock.closed)

    def test_each_imap_io_rebinds_socket_to_the_decreasing_total_budget(self):
        """NOOP、SEARCH 和每次 FETCH 都必须重新使用同一个总截止时间。"""
        target = "alias@icloud.com"

        class FakeClock:
            def __init__(self):
                self.now = 100.0

            def monotonic(self):
                return self.now

            def advance(self, seconds=0.01):
                self.now += seconds

        clock = FakeClock()

        class FakeSocket:
            def __init__(self):
                self.timeout = 30.0

            def settimeout(self, timeout):
                self.timeout = timeout

        class TimedIMAP(ScriptedIMAP):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.sock = FakeSocket()

            def noop(self):
                result = super().noop()
                clock.advance(min(0.01, self.sock.timeout))
                return result

            def uid(self, command, *args):
                result = super().uid(command, *args)
                clock.advance(min(0.01, self.sock.timeout))
                return result

        imap = TimedIMAP(
            snapshots=[[b"60", b"61"]],
            messages={
                b"60": otp_mail(target, "634567"),
                b"61": otp_mail("other@icloud.com", "623456"),
            },
        )
        pool = self.pool(wait_timeout=0.08, request_timeout=30)
        bound_timeouts: list[float] = []

        with patch.object(pool, "_connect_imap", return_value=imap), patch.object(
            pool,
            "_bound_socket_timeout",
            side_effect=lambda _imap, timeout: bound_timeouts.append(timeout),
        ), patch("core.icloud_mail_pool.time.monotonic", side_effect=clock.monotonic):
            code = pool.wait_for_code(self.mailbox(target))

        self.assertEqual(code, "634567")
        self.assertGreaterEqual(len(bound_timeouts), 4)
        self.assertTrue(all(a > b for a, b in zip(bound_timeouts, bound_timeouts[1:])))
        self.assertTrue(all(0 < timeout <= 0.08 for timeout in bound_timeouts))

    def test_budget_exhaustion_stops_fetching_and_sleep_cannot_cross_deadline(self):
        target = "alias@icloud.com"

        class FakeClock:
            def __init__(self):
                self.now = 200.0
                self.sleeps: list[float] = []

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.sleeps.append(seconds)
                self.now += seconds

            def advance(self, seconds=0.01):
                self.now += seconds

        clock = FakeClock()

        class FakeSocket:
            def __init__(self):
                self.timeout = 30.0

            def settimeout(self, timeout):
                self.timeout = timeout

        class TimedIMAP(ScriptedIMAP):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.sock = FakeSocket()

            def noop(self):
                result = super().noop()
                clock.advance(min(0.01, self.sock.timeout))
                return result

            def uid(self, command, *args):
                result = super().uid(command, *args)
                clock.advance(min(0.01, self.sock.timeout))
                return result

        imap = TimedIMAP(
            snapshots=[[b"70", b"71", b"72"]],
            messages={
                b"70": otp_mail("other-a@icloud.com", "712345"),
                b"71": otp_mail("other-b@icloud.com", "723456"),
                b"72": otp_mail("other-c@icloud.com", "734567"),
            },
        )
        pool = self.pool(wait_timeout=0.025, wait_interval=5, request_timeout=30)

        with patch.object(pool, "_connect_imap", return_value=imap), patch(
            "core.icloud_mail_pool.time.monotonic", side_effect=clock.monotonic
        ), patch("core.icloud_mail_pool.time.sleep", side_effect=clock.sleep):
            code = pool.wait_for_code(self.mailbox(target))

        fetches = [call for call in imap.uid_calls if call[0] == "fetch"]
        self.assertIsNone(code)
        self.assertEqual(len(fetches), 1)
        self.assertTrue(all(seconds <= 0.025 for seconds in clock.sleeps))
        self.assertLessEqual(clock.now, 200.025 + 1e-9)

    def test_pool_claim_and_success_state_are_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            pool = ICloudMailboxPool(
                {"mailboxes": "first@icloud.com\nsecond@icloud.com"},
                state_file,
            )
            with patch("core.icloud_mail_pool.secrets.choice", side_effect=lambda values: values[0]):
                mailbox = pool.acquire()
                pool.finish(mailbox, True)

            self.assertEqual(mailbox["address"], "first@icloud.com")
            with patch("core.icloud_mail_pool.secrets.choice", side_effect=lambda values: values[0]):
                self.assertEqual(pool.acquire()["address"], "second@icloud.com")

    def test_email_provider_accepts_icloud_source(self):
        self.assertEqual(parse_email_sources("icloud,outlook"), ["icloud", "outlook"])


if __name__ == "__main__":
    unittest.main()
