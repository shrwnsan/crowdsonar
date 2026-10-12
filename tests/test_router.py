"""Freshness router: the full decision table, plus CLI wiring under `auto`."""

import copy
from datetime import datetime, timezone

import crowdsonar.cli as cli
from crowdsonar.ingest import IngestResult, RawPost
from crowdsonar.router import DEFAULT_FRESHNESS_HOURS, assess, decide_overlay


def _post(age_hours: float) -> RawPost:
    created = datetime.now(timezone.utc).timestamp() - age_hours * 3600
    return RawPost(source="arctic", post_id="1a", post_title="t", post_url="u",
                   post_subreddit="s", post_author="a", post_score=1,
                   post_created_utc=created, post_body="b")


class TestAssess:
    def test_newest_item_wins(self):
        report = assess([_post(50), _post(3)], [])
        assert report.items == 2
        assert 2.5 < report.age_hours < 3.5

    def test_empty_and_error_counts(self):
        report = assess([], ["e1", "e2"])
        assert report.items == 0 and report.errors == 2
        assert report.age_hours is None


class TestDecisionTable:
    def test_fresh_archive_skips_overlay(self):
        d = decide_overlay(assess([_post(3)], []))
        assert d.overlay is False and not d.fallback
        assert "archive fresh" in d.reason and "3" in d.reason

    def test_stale_archive_runs_overlay(self):
        d = decide_overlay(assess([_post(51)], []))
        assert d.overlay is True and not d.fallback
        assert "archive stale" in d.reason

    def test_custom_threshold(self):
        d = decide_overlay(assess([_post(5)], []), stale_after_hours=2)
        assert d.overlay is True and "2h threshold" in d.reason

    def test_empty_window_is_fallback(self):
        d = decide_overlay(assess([], []))
        assert d.overlay is True and d.fallback is True
        assert "returned nothing" in d.reason

    def test_errored_archive_is_fallback(self):
        d = decide_overlay(assess([], ["e", "e", "e"]))
        assert d.overlay is True and d.fallback is True
        assert "archive errored" in d.reason

    def test_default_threshold_is_48h(self):
        assert DEFAULT_FRESHNESS_HOURS == 48.0


class _FakePrawClient:
    def __getattr__(self, name):
        raise AssertionError("no network in tests")


class TestCliAutoRouting:
    """The `auto` path must consult the router before touching praw."""

    CFG = {
        "name": "topic",
        "sources": {"reddit": {
            "subreddits": ["s"], "keywords": ["kw"], "time_filter": "week",
            "post_limit": 5, "comments_per_post": 0,
        }},
        "signal_types": [], "synthesis": {},
    }

    def _run(self, monkeypatch, arctic_result, freshness_hours=None):
        cfg = copy.deepcopy(self.CFG)
        if freshness_hours:
            cfg["sources"]["reddit"]["freshness_hours"] = freshness_hours

        monkeypatch.setenv("REDDIT_CLIENT_ID", "x")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "y")
        calls = {"arctic": 0, "praw": 0}

        monkeypatch.setattr(cli, "load_topic", lambda name, d=None: cfg)
        monkeypatch.setattr(cli, "ingest_arctic", lambda **kw: arctic_result)
        monkeypatch.setattr(cli, "save_raw_posts", lambda posts, t, r: ("path", len(posts)))
        monkeypatch.setattr(cli, "pending_classifications", lambda t: [])
        monkeypatch.setattr(cli, "create_praw_client", lambda **kw: _FakePrawClient())

        def fake_praw(**kw):
            calls["praw"] += 1
            return IngestResult()

        monkeypatch.setattr(cli, "ingest_praw", fake_praw)

        argv = ["crowdsonar", "--topic", "topic", "--dry-run"]
        monkeypatch.setattr("sys.argv", argv)
        cli.main()
        return calls

    def test_fresh_archive_never_touches_praw(self, monkeypatch, capsys):
        calls = self._run(monkeypatch, IngestResult(posts=[_post(3)]))
        assert calls["praw"] == 0
        assert "[DRY RUN] 1 items" in capsys.readouterr().out

    def test_stale_archive_runs_overlay(self, monkeypatch, capsys):
        calls = self._run(monkeypatch, IngestResult(posts=[_post(60)]))
        assert calls["praw"] == 1

    def test_custom_freshness_hours_honored(self, monkeypatch):
        calls = self._run(monkeypatch, IngestResult(posts=[_post(5)]), freshness_hours=2)
        assert calls["praw"] == 1

    def test_arctic_only_flag_skips_router_and_praw(self, monkeypatch):
        import argparse
        orig_parse = cli.parse_args

        def forced_arctic():
            args = orig_parse()
            args.source = "arctic"
            return args

        monkeypatch.setattr(cli, "parse_args", forced_arctic)
        monkeypatch.setenv("REDDIT_CLIENT_ID", "x")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "y")
        monkeypatch.setattr(cli, "load_topic", lambda name, d=None: self.CFG)
        monkeypatch.setattr(cli, "ingest_arctic", lambda **kw: IngestResult(posts=[_post(90)]))
        monkeypatch.setattr(cli, "save_raw_posts", lambda posts, t, r: ("path", len(posts)))
        monkeypatch.setattr(cli, "pending_classifications", lambda t: [])

        def boom(**kw):
            raise AssertionError("praw must not run under --source arctic")

        monkeypatch.setattr(cli, "ingest_praw", boom)
        monkeypatch.setattr("sys.argv", ["crowdsonar", "--topic", "topic", "--dry-run"])
        cli.main()
