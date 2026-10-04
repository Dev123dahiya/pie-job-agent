from pathlib import Path

from integrations.browser.manager import launch_channels


def test_a_named_browser_is_the_only_one_tried() -> None:
    assert launch_channels("chrome") == ["chrome"]
    assert launch_channels("msedge") == ["msedge"]


def test_auto_tries_the_bundled_browser_then_edge_then_chrome() -> None:
    assert launch_channels("auto") == [None, "msedge", "chrome"]


def test_the_login_script_uses_the_same_setting() -> None:
    import inspect

    from backend.scripts import login

    assert "launch_channels(get_settings().browser_channel)" in inspect.getsource(login._open)


def test_the_login_window_is_a_plain_browser(tmp_path: Path) -> None:

    from backend.scripts.login import plain_browser_command

    cmd = plain_browser_command(Path("chrome.exe"), tmp_path, "https://example.invalid/login")
    assert cmd[0] == "chrome.exe" and f"--user-data-dir={tmp_path}" in cmd
    # nothing that drives the browser or changes how it presents itself
    joined = " ".join(cmd)
    for flag in ("remote-debugging", "enable-automation", "disable-blink-features", "user-agent"):
        assert flag not in joined
