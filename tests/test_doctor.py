"""doctor: backend health matrix, keyless via mocked HTTP."""

import httpx
import pytest

import crowdsonar.doctor as doctor
from crowdsonar.doctor import check_arctic, check_llm, check_reddit_creds, check_storage, run_doctor


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


def _mock_get(handler, monkeypatch):
    monkeypatch.setattr(doctor.httpx, "get",
                        lambda url, **kw: handler(url, kw))


class TestArctic:
    def test_ok_reports_latency_and_quota(self, monkeypatch):
        def handler(url, kw):
            assert kw["params"] == doctor.ARCTIC_PROBE_PARAMS
            return httpx.Response(200, json={"data": [{"id": "1"}]},
                                  headers={"X-RateLimit-Remaining": "99"})

        _mock_get(handler, monkeypatch)
        r = check_arctic()
        assert r.status == "ok"
        assert "reachable" in r.detail and "99" in r.detail

    def test_unreachable_fails(self, monkeypatch):
        def handler(url, kw):
            raise httpx.ConnectError("no dns")

        _mock_get(handler, monkeypatch)
        assert check_arctic().status == "fail"

    def test_rate_limited_fails(self, monkeypatch):
        _mock_get(lambda url, kw: httpx.Response(429, headers={"X-RateLimit-Reset": "7"}), monkeypatch)
        r = check_arctic()
        assert r.status == "fail" and "7" in r.detail

    def test_api_error_body_fails(self, monkeypatch):
        _mock_get(lambda url, kw: httpx.Response(200, json={"error": "bad query"}), monkeypatch)
        assert "bad query" in check_arctic().detail


class TestReddit:
    def test_missing_creds_warns_never_fails(self):
        assert check_reddit_creds().status == "warn"

    def test_bad_creds_warn(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "x")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "y")
        monkeypatch.setattr("crowdsonar.ingest.create_praw_client",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("401")))
        assert check_reddit_creds().status == "warn"


class TestLLM:
    def test_missing_key_warns(self):
        assert check_llm().status == "warn"

    def test_rejected_key_fails(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "placeholder")

        def handler(url, kw):
            assert kw["headers"]["Authorization"].startswith("Bearer ")
            return httpx.Response(401)

        _mock_get(handler, monkeypatch)
        assert check_llm().status == "fail"

    def test_valid_endpoint_ok(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "placeholder")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example/v1")

        seen = {}
        def handler(url, kw):
            seen["url"] = url
            return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})

        _mock_get(handler, monkeypatch)
        r = check_llm()
        assert r.status == "ok" and "2 models" in r.detail
        assert seen["url"] == "https://llm.example/v1/models"


class TestStorageAndExitCode:
    def test_storage_ok_on_tmp(self, data_dir):
        r = check_storage()
        assert r.status == "ok" and "writable" in r.detail

    def test_exit_code_reflects_failures(self, data_dir, monkeypatch, capsys):
        monkeypatch.setattr(doctor, "check_arctic",
                            lambda: doctor.CheckResult("arctic", "fail", "down"))
        monkeypatch.setattr(doctor, "check_reddit_creds",
                            lambda: doctor.CheckResult("reddit", "warn", "unset"))
        monkeypatch.setattr(doctor, "check_llm",
                            lambda: doctor.CheckResult("llm", "ok", "fine"))
        monkeypatch.setattr(doctor, "check_storage",
                            lambda: doctor.CheckResult("storage", "ok", "fine"))
        assert run_doctor() == 1
        assert "FAIL" in capsys.readouterr().out

        monkeypatch.setattr(doctor, "check_arctic",
                            lambda: doctor.CheckResult("arctic", "ok", "up"))
        assert run_doctor() == 0
