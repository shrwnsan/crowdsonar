"""Freshness router: the decision table, plus CLI wiring under `auto`."""

import copy
import logging

import crowdsonar.cli as cli
from crowdsonar.arctic_shift import CoverageProbe
from crowdsonar.ingest import IngestResult
from crowdsonar.router import (
    DEFAULT_FRESHNESS_HOURS,
    CoverageReport,
    decide_overlay,
)


def _report(**kw) -> CoverageReport:
    return CoverageReport(**kw)


class TestDecisionTable:
    def test_fully_covered_archive_skips_overlay(self):
        d = decide_overlay(_report(subs_probed=2, subs_covered=2))
        assert d.overlay is False and not d.fallback
        assert "archive fresh" in d.reason and "2/2" in d.reason

    def test_partially_stale_archive_tops_up(self):
        d = decide_overlay(_report(subs_probed=3, subs_covered=2, matched_items=5))
        assert d.overlay is True and not d.fallback
        assert "archive stale" in d.reason and "1/3" in d.reason

    def test_probe_errors_noted_in_reason(self):
        d = decide_overlay(_report(subs_probed=2, subs_covered=1,
                                   probe_errors=1, matched_items=4))
        assert d.overlay is True
        assert "1 probe error" in d.reason

    def test_nothing_delivered_is_fallback(self):
        d = decide_overlay(_report(subs_probed=2, subs_covered=0, matched_items=0))
        assert d.overlay is True and d.fallback is True

    def test_quiet_topic_with_partial_coverage_is_not_fallback(self):
        # The archive reached the recent tail on at least one subreddit and
        # delivered matched items: a quiet keyword is not an outage. Top up,
        # but stay at INFO — this is normal operation.
        d = decide_overlay(_report(subs_probed=2, subs_covered=1, matched_items=0))
        assert d.overlay is True and d.fallback is False

    def test_ingest_errors_upgrade_to_fallback(self):
        d = decide_overlay(_report(subs_probed=2, subs_covered=1,
                                   matched_items=0, ingest_errors=2))
        assert d.overlay is True and d.fallback is True

    def test_all_probes_failed_with_items_tops_up(self):
        d = decide_overlay(_report(subs_probed=0, probe_errors=2, matched_items=7))
        assert d.overlay is True and d.fallback is False
        assert "coverage unverifiable" in d.reason

    def test_all_probes_failed_with_nothing_is_fallback(self):
        d = decide_overlay(_report(subs_probed=0, matched_items=0))
        assert d.overlay is True and d.fallback is True

    def test_default_threshold_is_48h(self):
        assert DEFAULT_FRESHNESS_HOURS == 48.0


class _FakePrawClient:
    def __getattr__(self, name):
        raise AssertionError("no network in tests")


class TestCliAutoRouting:
    """The `auto` path must consult coverage probes before touching praw."""

    CFG = {
        "name": "topic",
        "sources": {"reddit": {
            "subreddits": ["s"], "keywords": ["kw"], "time_filter": "week",
            "post_limit": 5, "comments_per_post": 0,
        }},
        "signal_types": [], "synthesis": {},
    }

    def _run(self, monkeypatch, probes, freshness_hours=None,
             overlay=None, source=None):
        cfg = copy.deepcopy(self.CFG)
        if freshness_hours:
            cfg["sources"]["reddit"]["freshness_hours"] = freshness_hours

        monkeypatch.setenv("REDDIT_CLIENT_ID", "x")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "y")
        calls = {"probes": 0, "praw": 0, "hours": None}

        monkeypatch.setattr(cli, "load_topic", lambda name, d=None: cfg)
        monkeypatch.setattr(cli, "ingest_arctic", lambda **kw: IngestResult())
        monkeypatch.setattr(cli, "save_raw_posts", lambda posts, t, r: ("path", len(posts)))
        monkeypatch.setattr(cli, "pending_classifications", lambda t: [])
        monkeypatch.setattr(cli, "create_praw_client", lambda **kw: _FakePrawClient())

        def fake_probes(subs, hours, base_url=None):
            calls["probes"] += 1
            calls["hours"] = hours
            return probes

        monkeypatch.setattr(cli, "probe_coverage", fake_probes)

        def fake_praw(**kw):
            calls["praw"] += 1
            return IngestResult()

        monkeypatch.setattr(cli, "ingest_praw", fake_praw)

        argv = ["crowdsonar", "--topic", "topic", "--dry-run"]
        if overlay:
            argv += ["--overlay", overlay]
        if source:
            argv += ["--source", source]
        monkeypatch.setattr("sys.argv", argv)
        cli.main()
        return calls

    def test_fresh_archive_never_touches_praw(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        probes = [CoverageProbe(subreddit="s", covered=True)]
        calls = self._run(monkeypatch, probes)
        assert calls["praw"] == 0 and calls["probes"] == 1
        phase1b = [r for r in caplog.records if "Phase 1b" in r.getMessage()]
        assert phase1b and phase1b[0].levelno == logging.INFO
        assert "SKIPPED" in phase1b[0].getMessage()

    def test_stale_archive_runs_overlay(self, monkeypatch):
        probes = [CoverageProbe(subreddit="s", covered=False)]
        calls = self._run(monkeypatch, probes)
        assert calls["praw"] == 1

    def test_custom_freshness_hours_honored(self, monkeypatch):
        calls = self._run(monkeypatch, [CoverageProbe("s", covered=False)],
                          freshness_hours=2)
        assert calls["hours"] == 2

    def test_overlay_always_skips_probes(self, monkeypatch):
        # Legacy composition: overlay whenever creds are set, no probe cost.
        calls = self._run(monkeypatch, [], overlay="always")
        assert calls["probes"] == 0 and calls["praw"] == 1

    def test_arctic_only_flag_skips_router_and_praw(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "x")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "y")
        monkeypatch.setattr(cli, "load_topic", lambda name, d=None: self.CFG)
        monkeypatch.setattr(cli, "ingest_arctic", lambda **kw: IngestResult())
        monkeypatch.setattr(cli, "save_raw_posts", lambda posts, t, r: ("path", len(posts)))
        monkeypatch.setattr(cli, "pending_classifications", lambda t: [])

        def boom(**kw):
            raise AssertionError("praw must not run under --source arctic")

        monkeypatch.setattr(cli, "ingest_praw", boom)
        monkeypatch.setattr("sys.argv", ["crowdsonar", "--topic", "topic",
                                         "--dry-run", "--source", "arctic"])
        cli.main()

    def test_fallback_decision_logs_at_warning(self, monkeypatch, caplog):
        # Probe failed AND the archive pass delivered nothing — the run may
        # bank nothing at all, so the fallback must not hide behind INFO.
        probes = [CoverageProbe(subreddit="s", covered=False, error="ConnectError")]
        self._run(monkeypatch, probes)
        phase1b = [r for r in caplog.records if "Phase 1b" in r.getMessage()]
        assert phase1b and phase1b[0].levelno == logging.WARNING
