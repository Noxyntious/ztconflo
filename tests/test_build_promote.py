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



def test_pmbootstrap_device_follows_the_device_package():
    from ztconflo.build import pmbootstrap_device
    from ztconflo.registry import Entry
    base = {"device": {"codename": "xiaomi-daisy", "origin": "pmos", "status": "untested"}, "boot": {}, "kernel": {}}
    generic = Entry({**base, "source": {"pmos": {"device_pkg": "device-qcom-msm8953"}}})
    assert pmbootstrap_device(generic) == "qcom-msm8953"
    assert pmbootstrap_device(Entry({**base, "source": {"pmos": {}}})) == "xiaomi-daisy"
    assert pmbootstrap_device(Entry(base)) == "xiaomi-daisy"



def test_dry_run_installs_the_ztos_system_overlay(tmp_path):
    # BETA-1's greetd config, user setup and fonts live in os/overlay; without
    # them greetd starts ztMETRO as its own user and the session dies at once
    reg = Registry(tmp_path / "registry")
    s = Settings(registry=reg.root, pmaports=FIX, out=tmp_path / "out", work=tmp_path / "work", dry_run=True)
    run = Runner(dry_run=True, log=lambda _: None)
    build(resolve("oneplus-enchilada", reg, Pmaports(FIX)), s, reg, run)
    flat = [" ".join(map(str, c)) for c in run.commands]
    copy = next(i for i, c in enumerate(flat) if "cp -a" in c and "os/overlay/." in c)
    config = next(i for i, c in enumerate(flat) if "useradd" in c)
    assert copy < config


def test_dry_run_installs_the_logind_power_key_drop_in(tmp_path):
    # without it logind powers the phone off on a short press of the power key
    reg = Registry(tmp_path / "registry")
    s = Settings(registry=reg.root, pmaports=FIX, out=tmp_path / "out", work=tmp_path / "work", dry_run=True)
    run = Runner(dry_run=True, log=lambda _: None)
    build(resolve("oneplus-enchilada", reg, Pmaports(FIX)), s, reg, run)
    flat = [" ".join(map(str, c)) for c in run.commands]
    assert any("install" in c and c.endswith("etc/systemd/logind.conf.d/10-zboot.conf") for c in flat)



def test_dry_run_builds_ztosial_before_ztmetro(tmp_path):
    # ztMETRO only uses ztOSIAL when ztos-ial.pc exists as it is built
    reg = Registry(tmp_path / "registry")
    s = Settings(registry=reg.root, pmaports=FIX, out=tmp_path / "out", work=tmp_path / "work", dry_run=True)
    run = Runner(dry_run=True, log=lambda _: None)
    build(resolve("oneplus-enchilada", reg, Pmaports(FIX)), s, reg, run)
    flat = [" ".join(map(str, c)) for c in run.commands]
    assert any("pacstrap" in c and "python-gobject" in c for c in flat)
    script = next(c for c in flat if "cd /usr/src/ztos/ztmetro" in c)
    assert script.index("cd /usr/src/ztos/ial") < script.index("cd /usr/src/ztos/ztmetro")
    assert any("systemctl enable ztos-iald" in c for c in flat)


def test_dry_run_installs_the_android_runtime(tmp_path):
    # ATL runs Android apps without binder: the ztOS installer ships it, and so must a ztConflo build
    reg = Registry(tmp_path / "registry")
    s = Settings(registry=reg.root, pmaports=FIX, out=tmp_path / "out", work=tmp_path / "work", dry_run=True)
    run = Runner(dry_run=True, log=lambda _: None)
    build(resolve("oneplus-enchilada", reg, Pmaports(FIX)), s, reg, run)
    flat = [" ".join(map(str, c)) for c in run.commands]
    copy = next(i for i, c in enumerate(flat) if "ztOSAndroidRuntimeV1/packages/" in c)
    install = next(i for i, c in enumerate(flat) if "android-runtime/install-runtime.sh /usr/local" in c)
    assert copy < install
    # the build's root is no mount point: pacman's space check off, and back on however it ends
    assert "#CheckSpace" in flat[install] and "trap" in flat[install]
    # a fresh root has no keyring until configure: the install sets it up first
    assert flat[install].index("pacman-key --init") < flat[install].index("install-runtime.sh")


def test_without_the_android_runtime_the_build_goes_on(tmp_path):
    from ztconflo.rootfs import android_runtime
    lines = []
    run = Runner(dry_run=False, log=lines.append)
    android_runtime(run, tmp_path / "root", tmp_path / "old-checkout")
    assert run.commands == [] and "ATL" in lines[0]


def test_dry_run_installs_ztos_packages_and_apps(tmp_path):
    # os/packages/common is what every ztOS system carries (the default Terminal
    # tile runs gnome-terminal); the Welcome and Updates tiles need their apps
    src = tmp_path / "ztOSMobile"
    (src / "os/packages").mkdir(parents=True)
    (src / "os/packages/common").write_text("# ztOS\nbase\ngnome-terminal  # the Terminal tile\nbrightnessctl\n")
    reg = Registry(tmp_path / "registry")
    s = Settings(registry=reg.root, pmaports=FIX, out=tmp_path / "out", work=tmp_path / "work", dry_run=True,
                 ztos_src=src)
    run = Runner(dry_run=True, log=lambda _: None)
    build(resolve("oneplus-enchilada", reg, Pmaports(FIX)), s, reg, run)
    flat = [" ".join(map(str, c)) for c in run.commands]
    pacstrap = next(c for c in flat if "pacstrap" in c).split()
    assert "gnome-terminal" in pacstrap and "brightnessctl" in pacstrap
    assert pacstrap.count("base") == 1
    ztmetro = next(i for i, c in enumerate(flat) if "cd /usr/src/ztos/ztmetro" in c)
    apps = next(i for i, c in enumerate(flat) if "os/apps/welcome os/apps/updates" in c)
    assert ztmetro < apps


def test_pacman_sandbox_follows_the_kernels_landlock(tmp_path):
    # pacman 7 downloads nothing on a kernel without Landlock (pmOS's msm8953)
    from ztconflo.rootfs import landlock_missing, pacman_sandbox
    without, with_, none = tmp_path / "without", tmp_path / "with", tmp_path / "none"
    for tree, text in ((without, "CONFIG_LSM=\"landlock,yama\"\n# CONFIG_SECURITY_LANDLOCK is not set\n"),
                       (with_, "CONFIG_SECURITY_LANDLOCK=y\n")):
        (tree / "boot").mkdir(parents=True)
        (tree / "boot/config").write_text(text)
    none.mkdir()
    assert landlock_missing(without) is True
    assert landlock_missing(with_) is False
    assert landlock_missing(none) is None
    for tree in (with_, none):     # nothing to change: no command at all
        lines = []
        run = Runner(dry_run=False, log=lines.append)
        pacman_sandbox(run, tmp_path / "root", tree)
        assert run.commands == []
    assert "sandbox left as it is" in lines[0]


def test_dry_run_plans_the_pacman_sandbox(tmp_path):
    reg = Registry(tmp_path / "registry")
    s = Settings(registry=reg.root, pmaports=FIX, out=tmp_path / "out", work=tmp_path / "work", dry_run=True)
    run = Runner(dry_run=True, log=lambda _: None)
    build(resolve("oneplus-enchilada", reg, Pmaports(FIX)), s, reg, run)
    flat = [" ".join(map(str, c)) for c in run.commands]
    assert any("DisableSandboxFilesystem" in c and c.endswith("etc/pacman.conf") for c in flat)


def test_pacstrap_syncs_every_repo_the_root_lists(tmp_path):
    # Arch Linux ARM's pacman.conf lists aur; without its database the ATL
    # install's pacman -S stops with "could not find database"
    from ztconflo.rootfs import pacman_conf
    repos = [l for l in pacman_conf(tmp_path).read_text().splitlines() if l.startswith("[") and l != "[options]"]
    assert repos == ["[core]", "[extra]", "[alarm]", "[aur]"]


def test_dry_run_installs_the_rest_of_what_the_installer_does(tmp_path):
    # the browser, zrn-pushd, Selawik, the SDK, the wallpapers and ringtones and
    # the installer's services, as ztOSMobile's installer puts them on every phone
    reg = Registry(tmp_path / "registry")
    s = Settings(registry=reg.root, pmaports=FIX, out=tmp_path / "out", work=tmp_path / "work", dry_run=True)
    run = Runner(dry_run=True, log=lambda _: None)
    build(resolve("oneplus-enchilada", reg, Pmaports(FIX)), s, reg, run)
    flat = [" ".join(map(str, c)) for c in run.commands]
    parts = next(i for i, c in enumerate(flat) if "install-browser" in c)
    for piece in ("android/push install", "systemctl --global enable zrn-pushd", "fonts/selawik",
                  "/usr/local/lib/ztos-sdk", "/etc/skel/.local/share/ztos/zygote.py"):
        assert piece in flat[parts], piece
    assert any("rsync" in c and "android/push/" in c for c in flat[:parts])
    media = next(i for i, c in enumerate(flat) if "ztos-media.tar.xz.part*" in c and "tar -C" in c)
    config = next(i for i, c in enumerate(flat) if "useradd" in c)
    assert parts < config and media < config       # skel before the user is made
    for unit in ("systemd-resolved", "systemd-pstore", "ztos-pstore-keep", "ztos-update-check.timer",
                 "systemd-timesyncd", "ztoscorenetworking-mm"):
        assert unit in flat[config], unit
    enabled = [l for l in flat[config].splitlines() if l.startswith(("for u in", "systemctl enable"))]
    assert not any("ztoscoresensorframework" in l for l in enabled)    # the S9's hardware, its layer's to enable
    assert any(c.startswith("sudo ln -sfn /run/systemd/resolve/stub-resolv.conf") for c in flat[config:])


def test_without_ztos_media_the_build_goes_on(tmp_path):
    from ztconflo.rootfs import media
    lines = []
    run = Runner(dry_run=False, log=lines.append)
    media(run, tmp_path / "root", tmp_path / "old-checkout")
    assert run.commands == [] and "no wallpapers" in lines[0]
