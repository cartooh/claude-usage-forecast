from usage_forecast import setup

HOOK = "python3 /opt/cuf/hooks/statusline_hook.py"


def test_statusline_set_when_absent():
    new, action, orig = setup.plan_statusline_install({"theme": "dark"}, HOOK)
    assert action == "set" and new["statusLine"] == {"type": "command", "command": HOOK}
    assert new["theme"] == "dark" and orig is None


def test_statusline_wraps_existing_command():
    cur = {"type": "command", "command": "bash ~/my status.sh", "padding": 1}
    new, action, orig = setup.plan_statusline_install({"statusLine": cur}, HOOK)
    assert action == "wrapped" and orig == cur
    assert new["statusLine"]["command"] == HOOK + " --wrap 'bash ~/my status.sh'"
    assert new["statusLine"]["padding"] == 1


def test_statusline_left_alone_when_script_already_calls_hook(tmp_path):
    script = tmp_path / "sl.sh"
    script.write_text("#!/bin/bash\necho x | python3 /x/hooks/statusline_hook.py\n")
    settings = {"statusLine": {"type": "command", "command": "bash %s" % script}}
    new, action, _ = setup.plan_statusline_install(settings, HOOK)
    assert action == "already" and new is settings


def test_cron_install_replaces_old_lines_and_keeps_others():
    cur = ("0-59 * * * * wget -O x.log https://example.invalid/u\n"
           "*/30 * * * * cd /p && .venv/bin/python -m usage_forecast.collector >/dev/null 2>&1\n")
    new = setup.plan_cron(cur, True)
    lines = new.splitlines()
    assert lines[0].startswith("0-59") and len(lines) == 2
    assert lines[1].endswith(setup.CRON_MARK) and "run.sh" in lines[1]
    assert setup.plan_cron(new, True) == new                 # idempotent
    assert setup.plan_cron(new, False) == lines[0] + "\n"    # uninstall keeps other jobs


def test_cron_uninstall_on_empty_crontab():
    assert setup.plan_cron("", False) == ""
