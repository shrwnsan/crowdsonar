"""Provenance surfacing: ingest-mix counts + deterministic briefing footer."""

import httpx
import pytest

import crowdsonar.synthesize as synthesize
from crowdsonar.storage import ingest_mix, save_signals
from crowdsonar.synthesize import _provenance_footer, synthesize_briefing
from helpers import make_signal_row as _signal_row


def _seed(data_dir):
    rows = (
        [_signal_row({"source": "arctic", "post_id": f"a{i}", "comment_id": "",
                      "post_score": 10 + i, "post_title": f"T{i}"}) for i in range(9)]
        + [_signal_row({"source": "praw", "post_id": "p1", "comment_id": "",
                        "post_score": 5, "post_title": "P1"})]
    )
    return save_signals(rows, "topic", "run1")


class TestIngestMix:
    def test_counts_per_source(self, data_dir):
        _seed(data_dir)
        assert ingest_mix("topic") == {"arctic": 9, "praw": 1}

    def test_empty_topic(self, data_dir):
        assert ingest_mix("nothing") == {}


class TestFooter:
    def test_format_and_ordering(self):
        footer = _provenance_footer({"arctic": 116, "praw": 7}, "past 7 days")
        assert footer == ("---\n*Ingest mix (past 7 days): "
                          "arctic (public archive) 94% · praw (real-time) 6% — n=123 signals*")

    def test_unknown_source_gets_plain_label(self):
        footer = _provenance_footer({"mystery": 4}, "past 7 days")
        assert "mystery 100%" in footer

    def test_empty_mix_no_footer(self):
        assert _provenance_footer({}, "past 7 days") == ""


class TestSynthesizeProvenance:
    @pytest.fixture
    def briefed(self, data_dir, monkeypatch):
        """Run synthesize_briefing against a mocked LLM endpoint."""
        _seed(data_dir)
        monkeypatch.setenv("OPENAI_API_KEY", "placeholder")

        captured = {}

        def fake_post(url, **kw):
            captured["url"] = url
            captured["payload"] = kw["json"]
            req = httpx.Request("POST", url, json=kw["json"])
            return httpx.Response(200, request=req, json={
                "choices": [{"message": {"content": "## 1. Overview\nSomething is happening."}}]
            })

        monkeypatch.setattr(synthesize.httpx, "post", fake_post)
        out = synthesize_briefing(topic="topic", topic_cfg={"name": "topic"}, days=7)
        return out, captured

    def test_footer_appended_after_llm_output(self, briefed):
        out, _ = briefed
        assert out.startswith("## 1. Overview")
        assert out.endswith("arctic (public archive) 90% · praw (real-time) 10% — n=10 signals*")

    def test_user_msg_carries_ingest_mix(self, briefed):
        _, captured = briefed
        user_msg = captured["payload"]["messages"][1]["content"]
        assert '"ingest_mix": {"arctic": 9, "praw": 1}' in user_msg
