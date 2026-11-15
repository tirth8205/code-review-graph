"""Native daemon config watcher accepts files published by atomic rename."""

import threading

from code_review_graph.daemon import ConfigWatcher


def test_atomic_config_replacement_triggers_reload(tmp_path):
    config = tmp_path / "watch.toml"
    config.write_text('[daemon]\nsession_name = "old"\n', encoding="utf-8")
    reloaded = threading.Event()
    watcher = ConfigWatcher(config, reloaded.set)
    watcher.start()
    try:
        candidate = config.with_suffix(".new")
        candidate.write_text('[daemon]\nsession_name = "new"\n', encoding="utf-8")
        candidate.replace(config)
        assert reloaded.wait(5), "the watcher did not see the config replacement"
    finally:
        watcher.stop()
