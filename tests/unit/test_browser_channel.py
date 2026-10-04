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
