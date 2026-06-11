"""safe_for_log のユニットテスト (CodeQL py/log-injection 対策)"""

from alpha_strike.log_sanitize import safe_for_log


class TestSafeForLog:
    def test_plain_string_unchanged(self):
        assert safe_for_log("hello") == "hello"

    def test_lf_removed(self):
        assert safe_for_log("line1\nline2") == "line1line2"

    def test_crlf_removed(self):
        assert safe_for_log("line1\r\nline2") == "line1line2"

    def test_tab_removed(self):
        assert safe_for_log("col1\tcol2") == "col1col2"

    def test_null_byte_removed(self):
        assert safe_for_log("safe\x00malicious") == "safemalicious"

    def test_all_control_chars_removed(self):
        # 0x00-0x1F の全制御文字 + 0x7F
        ctrl_chars = "".join(chr(c) for c in range(0x20)) + "\x7f"
        s = "OK" + ctrl_chars + "END"
        assert safe_for_log(s) == "OKEND"

    def test_unicode_preserved(self):
        # 日本語は制御文字ではないので残る
        assert safe_for_log("注文成功") == "注文成功"

    def test_truncation_at_max_len(self):
        s = "a" * 200
        result = safe_for_log(s, max_len=50)
        assert result == "a" * 50 + "..."

    def test_truncation_does_not_apply_below_max(self):
        s = "a" * 50
        result = safe_for_log(s, max_len=50)
        assert result == s

    def test_non_string_converted_via_str(self):
        assert safe_for_log(42) == "42"
        assert safe_for_log(3.14) == "3.14"
        assert safe_for_log(None) == "None"

    def test_exception_object_sanitized(self):
        e = ValueError("error\nmessage")
        assert safe_for_log(e) == "errormessage"

    def test_log_injection_payload_neutralized(self):
        """典型的な log injection 攻撃ペイロードが無害化されること"""
        attack = "user\n[CRITICAL] 攻撃者が偽のログを注入"
        result = safe_for_log(attack)
        # 改行が消えるので「[CRITICAL]」が同じ行に残るが、ログレベルとしてのプレフィックスは作れない
        assert "\n" not in result
        assert "\r" not in result
