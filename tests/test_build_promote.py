# SPDX-License-Identifier: GPL-2.0-or-later
"""The build plan (dry run), the CLI's flag handling, and the checklist rules."""
from pathlib import Path

import pytest

from ztconflo import Exit
from ztconflo.build import build
from ztconflo.cli import main
from ztconflo.config import Settings
from ztconflo.pmos import Pmaports
from ztconflo.promote import CHECKS, Phone, new_status, run_checks
from ztconflo.registry import Registry
from ztconflo.resolve import resolve
from ztconflo.runner import Runner

FIX = Path(__file__).parent / "fixtures" / "pmaports"


def test_dry_run_plans_every_step_and_writes_nothing(tmp_path):
    reg = Registry(tmp_path / "registry")
    s = Settings(registry=reg.root, pmaports=FIX, out=tmp_path / "out", work=tmp_path / "work", dry_run=True)
    run = Runner(dry_run=True, log=lambda _: None)
    build(resolve("oneplus-enchilada", reg, Pmaports(FIX)), s, reg, run)
    flat = [" ".join(c) for c in run.commands]
    assert any("pmbootstrap" in c and "linux-postmarketos-qcom-sdm845" in c for c in flat)
    assert any("pacstrap" in c and "mesa" in c for c in flat)
    assert any("make install" in c for c in flat)
    assert any("mke2fs" in c and "pmOS_root" in c for c in flat)
    assert any(c.startswith("img2simg") for c in flat)
    assert any(c.startswith("mkbootimg") and "--pagesize 4096" in c for c in flat)
    assert not (tmp_path / "registry").exists() and not (tmp_path / "out").exists()


def test_flags_before_the_subcommand_are_kept(tmp_path, capsys):
    code = main(["--registry", str(tmp_path / "reg"), "--pmaports", str(FIX), "lookup", "enchilada"])
    assert code == 0 and "postmarketOS fallback" in capsys.readouterr().out
    assert main(["--pmaports", str(FIX), "--registry", str(tmp_path / "reg"), "lookup", "amazon-sheldon"]) == 2
    assert main(["--registry", str(tmp_path / "reg"), "lookup", "nosuchphone", "--pmaports", str(FIX)]) == 1


def test_dry_run_build_through_the_cli(tmp_path):
    assert main(["--registry", str(tmp_path / "reg"), "--pmaports", str(FIX), "--dry-run",
                 "build", "oneplus-enchilada", "--out", str(tmp_path / "out")]) == 0
    assert not (tmp_path / "reg").exists()


class FakePhone(Phone):
    def __init__(self, answers):
        self.target = "alarm@test"
        self.answers = answers

    def sh(self, command):
        for key, (code, out) in self.answers.items():
            if key in command:
                return code, out
        return 0, ""


GOOD = {"pgrep -x ztmetro": (0, "3\n"), "/sys/class/drm": (0, "1080x2280\n"),
        "renderer: ": (0, "renderer: FD630, OpenGL ES 3.2\n"), "nmcli": (0, "4\n"),
        "capabilities/ff": (0, "/sys/class/input/event2/device/capabilities/ff\n"),
        "capacity": (0, "87\n"), "mmcli": (0, "0\n")}


def test_all_good_phone_is_tested_without_a_modem():
    results = run_checks(FakePhone(GOOD), ask=lambda _: "y", log=lambda *_: None)
    assert results["modem"] == "absent"            # optional: recorded, not blocking
    assert new_status(results) == "tested"


def test_llvmpipe_is_only_boots():
    phone = FakePhone({**GOOD, "renderer: ": (0, "renderer: llvmpipe (LLVM 19)\n")})
    results = run_checks(phone, ask=lambda _: "y", log=lambda *_: None)
    assert results["gpu"] == "fail" and new_status(results) == "boots"


def test_no_touch_means_untested():
    results = run_checks(FakePhone(GOOD), ask=lambda q: "n" if "corner targets" in q else "y", log=lambda *_: None)
    assert new_status(results) == "untested"


def test_checklist_matches_the_spec():
    assert [c.key for c in CHECKS] == ["ztmetro", "display", "touch", "gpu", "wifi", "vibrator", "audio",
                                       "battery", "suspend", "modem", "ial", "android"]
    assert {c.key for c in CHECKS if c.needed_for == "boots"} == {"ztmetro", "display", "touch"}
    assert [c.key for c in CHECKS if c.optional] == ["modem", "android"]


def test_ial_and_android_rows_are_n_a_without_a_manifest():
    phone = FakePhone({**GOOD, "/vendor/ztos/manifest.toml": (1, "")})
    results = run_checks(phone, ask=lambda _: "y", log=lambda *_: None)
    assert results["ial"] == "n/a" and results["android"] == "n/a"
    assert new_status(results) == "tested"              # a pre-GSI image is not held back


def test_a_failing_ial_interface_stops_tested():
    phone = FakePhone({**GOOD, "ztos-ial test": (1, "Display1: FAIL brightness did not move\n")})
    results = run_checks(phone, ask=lambda _: "y", log=lambda *_: None)
    assert results["ial"] == "fail" and new_status(results) == "boots"


def test_android_apps_are_recorded_but_optional():
    phone = FakePhone({**GOOD, "waydroid shell pidof": (0, "4242\n")})
    results = run_checks(phone, ask=lambda _: "y", log=lambda *_: None)
    assert results["android"] == "pass"
    results = run_checks(FakePhone(GOOD), ask=lambda _: "y", log=lambda *_: None)
    assert results["android"] == "absent" and new_status(results) == "tested"


def test_native_entries_need_the_repository(tmp_path):
    from ztconflo.build import fetch_kernel
    from ztconflo.registry import Entry
    e = Entry({"device": {"codename": "x", "origin": "native", "status": "untested"}, "boot": {}, "kernel": {}})
    with pytest.raises(Exit) as err:
        fetch_kernel(Runner(dry_run=True, log=lambda _: None), e, Settings(), tmp_path)
    assert err.value.code == Exit.BUILD_FAILED


def test_promote_prepares_cannot_hang():
    # speaker-test was seen to ignore timeout's TERM after the tone: promote
    # waited forever. Every timeout in a prepare kills after it
    import re
    from ztconflo.promote import CHECKS
    for c in CHECKS:
        for t in re.findall(r"timeout [^ ]+(?: [^ ]+)?", c.prepare):
            assert t.startswith("timeout -k "), (c.key, t)
