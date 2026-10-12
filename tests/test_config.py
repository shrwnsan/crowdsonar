"""Topic config validation: the name keys every on-disk path, so lock it down."""

import pytest

from crowdsonar.config import _validate


def _cfg(name="example", **reddit_extra):
    reddit = {"subreddits": ["s"], "keywords": ["k"]}
    reddit.update(reddit_extra)
    return {"name": name, "sources": {"reddit": reddit},
            "signal_types": [], "synthesis": {}}


class TestNameValidation:
    def test_accepts_normal_names(self):
        for n in ["example", "my_topic", "topic-2", "v1.2", "UpCase"]:
            _validate(_cfg(name=n))

    @pytest.mark.parametrize("bad", [
        "../escape",       # path traversal out of data/raw/<topic>/
        "a/b",             # separator injection
        "quote'name",      # breaks out of DuckDB glob string literals
        "space name",
        "",                # empty → parquet names like "_run.parquet"
        "..",
    ])
    def test_rejects_unsafe_names(self, bad):
        with pytest.raises(ValueError, match="name must match"):
            _validate(_cfg(name=bad))
