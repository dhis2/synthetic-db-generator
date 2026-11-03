from dbgen import timing


def test_the_summary_holds_only_the_stages_asked_for(tmp_path):
    for stage in ("validate", "load", "generate"):
        with timing.timed(tmp_path, stage):
            pass
    summary = timing.summary(tmp_path, ["compile", "generate", "load", "dump"])
    assert [t["stage"] for t in summary] == ["generate", "load"]
