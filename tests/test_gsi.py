# SPDX-License-Identifier: GPL-2.0-or-later
"""ztOSIAL: the registry's new tables, the kernel contract, the generated
manifest, fetching the GSI, and the GSI pipeline's dry run."""
import hashlib
import io
import json
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

from ztconflo import Exit, contract, gsi, ial
from ztconflo.build import build, ial_check, kernel_contract
from ztconflo.cli import main
from ztconflo.config import Settings
from ztconflo.pmos import Pmaports
from ztconflo.registry import Entry, Registry, dumps, validate
from ztconflo.resolve import resolve
from ztconflo.runner import Runner

FIX = Path(__file__).parent / "fixtures"
PMAPORTS = FIX / "pmaports"
KCONFIG = FIX / "kconfig"


def entry(**extra):
    data = {
        "device": {"codename": "vendor-phone", "name": "Vendor Phone", "arch": "aarch64",
                   "status": "untested", "origin": "pmos"},
        "source": {"pmos": {"pmaports_commit": "abc", "device_pkg": "device-vendor-phone",
                            "kernel_pkg": "linux-x", "firmware_pkgs": [], "category": "testing"}},
        "kernel": {"mainline": True, "version": "6.18", "artifacts_sha256": "", "soc": "sdm845",
                   "gpu_driver": "freedreno"},
        "boot": {"flash_method": "fastboot", "rootfs_target": "userdata"},
        "userspace": {"daemons": ["rmtfs", "pd-mapper"]},
        "ztmetro": {"scale": 2.5, "vibrator": "auto", "rotation": 0},
        "verified": {"date": "", "by": "", "checklist": {}},
    }
    data.update(extra)
    return data


# ---- registry ------------------------------------------------------------------------------

def test_ial_tables_round_trip():
    data = entry(ial={"Display1": {"node": "/sys/class/backlight/backlight", "curve": "gamma:2.2"},
                      "Haptics1": {"backend": "generic", "device": "pmic_vib"}},
                 android={"binder": "binderfs", "gralloc": "gbm"})
    validate(data)
    text = dumps(data)
    assert tomllib.loads(text) == data
    assert text.index("[ial]") < text.index("[android]") < text.index("[verified]")


def test_native_source_round_trips():
    data = entry(source={"native": {"repo": "https://example.org/ztos-device-x", "commit": "f00"}})
    data["device"]["origin"] = "native"
    validate(data)
    assert tomllib.loads(dumps(data))["source"]["native"]["commit"] == "f00"


def test_old_entries_stay_valid():
    validate(entry())
    old = entry()
    old["device"]["origin"] = "native"
    del old["source"]
    validate(old)                 # a native entry from before [source.native]


@pytest.mark.parametrize("change, words", [
    ({"android": {"binder": "maybe"}}, "[android].binder"),
    ({"android": {"gralloc": 3}}, "[android].gralloc"),
    ({"ial": {"display": {"node": "x"}}}, "not an interface name"),
    ({"ial": {"Display1": "x"}}, "must be a table"),
    ({"ial": {"Modem1": {"backend": "own"}}}, "backend must be one of"),
    ({"ial": {"Modem1": {"backend": "vendor"}}}, "needs a module"),
    ({"ial": {"Modem1": {"backend": "service"}}}, "needs a service"),
    ({"source": {"pmos": {}, "native": {"repo": "r", "commit": "c"}}}, "only for origin"),
])
def test_bad_ial_tables_are_rejected(change, words):
    with pytest.raises(ValueError, match=words.replace("[", r"\[").replace("]", r"\]")):
        validate(entry(**change))


def test_native_source_needs_repo_and_commit():
    data = entry(source={"native": {"repo": "r"}})
    data["device"]["origin"] = "native"
    with pytest.raises(ValueError, match="commit"):
        validate(data)


# ---- kernel contract -----------------------------------------------------------------------

def test_a_full_config_meets_the_contract():
    r = contract.check(KCONFIG / "full.config", "freedreno")
    assert r.known and not r.hard_failures and not r.soft_gaps
    assert set(r.state.values()) == {"ok"}


def test_soft_gaps_are_named_not_fatal():
    r = contract.check(KCONFIG / "no-binder.config", "freedreno")
    assert not r.hard_failures
    assert {n.key for n in r.soft_gaps} == {"waydroid", "haptics"}
    assert r.missing["waydroid"] == ["ANDROID_BINDER_IPC", "ANDROID_BINDERFS"]
    assert r.has("usbnet")                      # RNDIS alone is enough


def test_hard_failures_cgroups_and_gpu():
    r = contract.check(KCONFIG / "no-memcg.config", "freedreno")
    assert {n.key for n in r.hard_failures} == {"systemd", "gpu"}
    assert r.missing["systemd"] == ["MEMCG"] and r.missing["gpu"] == ["DRM_MSM"]
    assert not contract.check(KCONFIG / "no-memcg.config", "panfrost").state["gpu"] == "missing"


def test_no_config_is_unknown_not_a_failure():
    r = contract.check(None, "freedreno")
    assert not r.known and not r.hard_failures and not r.soft_gaps
    assert r.has("waydroid")
    assert "unchecked" in r.lines()[0]


def test_no_gpu_driver_leaves_the_gpu_unknown():
    assert contract.check(KCONFIG / "full.config", None).state["gpu"] == "unknown"


def test_the_gsi_can_ask_for_more():
    r = contract.check(KCONFIG / "full.config", "freedreno", {"hard": ["CONFIG_EROFS_FS"], "soft": ["ZSWAP"]})
    assert [n.key for n in r.hard_failures] == ["gsi-hard"] and [n.key for n in r.soft_gaps] == ["gsi-soft"]


def test_config_is_found_where_kernel_packages_leave_it(tmp_path):
    (tmp_path / "boot").mkdir()
    assert contract.find_config(tmp_path) is None
    (tmp_path / "boot/config-6.18.1-sdm845").write_text("CONFIG_PSI=y\n")
    assert contract.find_config(tmp_path / "missing", tmp_path).name == "config-6.18.1-sdm845"
    import gzip
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/config.gz").write_bytes(gzip.compress(b"CONFIG_ZRAM=m\n"))
    assert contract.read_config(tmp_path / "proc/config.gz") == {"ZRAM": "m"}


def test_hard_failure_stops_with_exit_2(tmp_path):
    (tmp_path / "boot").mkdir()
    (tmp_path / "boot/config-x").write_text((KCONFIG / "no-memcg.config").read_text())
    run = Runner(dry_run=True, log=lambda _: None)
    with pytest.raises(Exit) as err:
        kernel_contract(run, Entry(entry()), (tmp_path,), None, Registry(tmp_path / "reg"))
    assert err.value.code == Exit.NOT_ELIGIBLE and "MEMCG" in str(err.value)


def test_soft_gaps_write_binder_none_into_the_entry(tmp_path):
    reg = Registry(tmp_path / "reg")
    path = reg.write(entry())
    e = reg.find("vendor-phone")
    (tmp_path / "boot").mkdir()
    (tmp_path / "boot/config-x").write_text((KCONFIG / "no-binder.config").read_text())
    said = []
    kernel_contract(Runner(log=said.append), e, (tmp_path,), None, reg)
    assert tomllib.loads(path.read_text())["android"] == {"binder": "none", "gralloc": "gbm"}
    assert any(line.startswith("!!! Waydroid") for line in said)


# ---- the generated manifest ----------------------------------------------------------------

class FakeGsi:
    version = "0.2.0"
    required = ["Device1", "Power1", "Perf1", "Display1", "Sleep1"]
    interfaces = {"Power1": 1, "Display1": 2}
    kernel_contract = {}


def test_manifest_declares_generic_backends():
    r = contract.check(KCONFIG / "full.config", "freedreno")
    m, notes = ial.manifest(entry(boot={"flash_method": "fastboot", "rootfs_target": "userdata",
                                        "usb_gadget_udc": "a600000.usb"}), r, FakeGsi())
    assert list(m["ial"]) == ["Device1", "Power1", "Perf1", "Display1", "Sleep1", "Sensors1", "Haptics1",
                              "Lights1", "Modem1"]
    assert all(t["backend"] == "generic" for t in m["ial"].values())
    assert m["ial"]["Display1"]["version"] == 2 and m["ial"]["Perf1"]["version"] == 1
    assert m["gsi"] == {"min": "0.2.0"} and m["android"] == {"binder": "binderfs", "gralloc": "gbm"}
    assert m["boot"] == {"usb_gadget_udc": "a600000.usb"}
    assert m["units"] == {"enable": ["rmtfs.service", "pd-mapper.service"]}
    assert notes == []


def test_overrides_from_the_entry_win():
    r = contract.check(KCONFIG / "full.config", "freedreno")
    e = entry(ial={"Display1": {"node": "/sys/class/backlight/backlight", "curve": "gamma:2.2"},
                   "Modem1": {"backend": "vendor", "module": "/vendor/ztos/ial/x.py"},
                   "CallAudio1": {"backend": "service", "service": "ztos-ial-callaudio-x.service"}})
    m, _ = ial.manifest(e, r, FakeGsi())
    assert m["ial"]["Display1"] == {"backend": "generic", "version": 2,
                                    "node": "/sys/class/backlight/backlight", "curve": "gamma:2.2"}
    assert m["ial"]["Modem1"]["backend"] == "vendor" and m["ial"]["CallAudio1"]["version"] == 1
    back = tomllib.loads(ial.dumps(m))
    assert back["ial"]["Display1"]["curve"] == "gamma:2.2" and back["device"]["codename"] == "vendor-phone"


def test_soft_gaps_hide_capabilities():
    r = contract.check(KCONFIG / "no-binder.config", "freedreno")
    m, notes = ial.manifest(entry(), r, FakeGsi())
    assert "Haptics1" not in m["ial"] and m["android"]["binder"] == "none"
    assert m["kernel"]["soft_gaps"] == ["waydroid", "haptics"]
    assert any("Waydroid hidden" in n for n in notes) and any("Haptics1" in n for n in notes)
    # a vendor haptics backend does not need the generic one's force feedback
    m, _ = ial.manifest(entry(ial={"Haptics1": {"backend": "vendor", "module": "/vendor/ztos/ial/h.py"}}), r, FakeGsi())
    assert m["ial"]["Haptics1"]["backend"] == "vendor"


def test_unknown_contract_keeps_the_entrys_binder():
    m, _ = ial.manifest(entry(android={"binder": "none", "gralloc": "minigbm"}), contract.check(None, "x"))
    assert m["android"] == {"binder": "none", "gralloc": "minigbm"} and "Haptics1" in m["ial"]


# ---- fetching the GSI ----------------------------------------------------------------------

def make_gsi(folder: Path, version="0.2.0", sidecar=True, name=None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    meta = {"version": version, "ial": {"required": ["Device1", "Power1"], "interfaces": {"Power1": 1}},
            "kernel_contract": {"hard": ["CGROUPS"]}}
    path = folder / (name or f"ztos-gsi-{version}-aarch64.tar.gz")
    with tarfile.open(path, "w:gz") as tar:
        for member, data in (("./usr/share/ztos/gsi.json", json.dumps(meta).encode()),
                             ("./usr/bin/ztos-ial", b"#!/bin/sh\nexit 0\n")):
            info = tarfile.TarInfo(member)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    if sidecar:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_name(path.name + ".sha256").write_text(f"{digest}  {path.name}\n")
    return path


def test_fetch_verifies_and_caches_a_local_gsi(tmp_path):
    src = make_gsi(tmp_path / "dl")
    cache = tmp_path / "cache"
    g = gsi.fetch(str(src), cache, log=lambda _: None)
    assert g.version == "0.2.0" and g.verified and g.required == ["Device1", "Power1"]
    assert g.path == cache / "0.2.0" / src.name and g.path.is_file()
    assert (cache / "0.2.0/gsi.json").is_file() and g.sha256 == hashlib.sha256(src.read_bytes()).hexdigest()
    again = gsi.fetch("0.2.0", cache, log=lambda _: None)      # by version: the cache, no download
    assert again.path == g.path and again.verified and again.sha256 == g.sha256
    assert again.record()["version"] == "0.2.0"


def test_a_mismatched_sha256_stops(tmp_path):
    src = make_gsi(tmp_path / "dl")
    src.with_name(src.name + ".sha256").write_text("0" * 64 + "\n")
    with pytest.raises(Exit) as err:
        gsi.fetch(str(src), tmp_path / "cache", log=lambda _: None)
    assert err.value.code == Exit.BUILD_FAILED and "published sha256" in str(err.value)
    assert not (tmp_path / "cache").exists()


def test_a_changed_cache_is_noticed(tmp_path):
    cache = tmp_path / "cache"
    g = gsi.fetch(str(make_gsi(tmp_path / "dl")), cache, log=lambda _: None)
    g.path.write_bytes(g.path.read_bytes() + b"\0")
    with pytest.raises(Exit, match="changed since"):
        gsi.fetch("0.2.0", cache, log=lambda _: None)


def test_no_sidecar_is_recorded_not_verified(tmp_path):
    said = []
    g = gsi.fetch(str(make_gsi(tmp_path / "dl", sidecar=False)), tmp_path / "cache", log=said.append)
    assert not g.verified and g.sha256 and any("not verified" in s for s in said)


def test_the_version_comes_from_gsi_json(tmp_path):
    g = gsi.fetch(str(make_gsi(tmp_path / "dl", "0.3.1", name="nightly.tar.gz")), tmp_path / "cache",
                  log=lambda _: None)
    assert g.version == "0.3.1" and (tmp_path / "cache/0.3.1/nightly.tar.gz").is_file()


def test_a_dry_run_caches_nothing(tmp_path):
    g = gsi.fetch(str(make_gsi(tmp_path / "dl")), tmp_path / "cache", dry_run=True, log=lambda _: None)
    assert g.version == "0.2.0" and not (tmp_path / "cache").exists()
    said = []
    g = gsi.fetch("9.9.9", tmp_path / "cache", dry_run=True, log=said.append)
    assert "would download" in said[0] and "ztos-gsi-9.9.9-aarch64.tar.zst" in said[0]


def test_not_a_gsi(tmp_path):
    bad = tmp_path / "ztos-gsi-1-aarch64.tar.gz"
    with tarfile.open(bad, "w:gz"):
        pass
    with pytest.raises(Exit, match="not a ztOS GSI"):
        gsi.fetch(str(bad), tmp_path / "cache", log=lambda _: None)
    with pytest.raises(Exit, match="no such file"):
        gsi.fetch(str(tmp_path / "nope.tar.zst"), tmp_path / "cache", log=lambda _: None)


def test_zstd_gsi(tmp_path):
    gz = make_gsi(tmp_path / "dl", sidecar=False)
    zst = gz.with_name("ztos-gsi-0.2.0-aarch64.tar.zst")
    r = subprocess.run(["sh", "-c", f"gzip -dc {gz} | zstd -q -o {zst}"], capture_output=True)
    if r.returncode != 0:
        pytest.skip("no zstd on this host")
    assert gsi.read_meta(zst)["version"] == "0.2.0"


# ---- the GSI pipeline ----------------------------------------------------------------------

def test_gsi_dry_run_plans_every_step_and_writes_nothing(tmp_path):
    src = make_gsi(tmp_path / "dl")
    reg = Registry(tmp_path / "registry")
    s = Settings(registry=reg.root, pmaports=PMAPORTS, out=tmp_path / "out", work=tmp_path / "work",
                 dry_run=True, gsi=str(src), gsi_cache=tmp_path / "cache")
    said = []
    run = Runner(dry_run=True, log=said.append)
    build(resolve("oneplus-enchilada", reg, Pmaports(PMAPORTS)), s, reg, run)
    flat = [" ".join(c) for c in run.commands]
    log = "\n".join(said)
    steps = [line for line in said if line.startswith("==> ")]
    assert [x.split()[1] for x in steps] == ["1.", "2.", "2b.", "3.", "4.", "5.", "6.", "7.", "8."]
    assert any(c.startswith("sudo tar") or c.startswith("tar") for c in flat if str(src.name) in c)
    assert any("pmbootstrap" in c and "linux-postmarketos-qcom-sdm845" in c for c in flat)
    assert not any("pacstrap" in c for c in flat)                  # the GSI replaces the per-phone root
    assert any("ztos-ial check --root /" in c for c in flat)
    assert any("mkbootimg" in c for c in flat)
    assert "kernel config not found" in log and '[ial.Display1]' in log
    assert "enable rmtfs.service pd-mapper.service tqftpserv.service" in log
    for written in ("registry", "out", "cache", "work"):
        assert not (tmp_path / written).exists(), written


def test_without_gsi_the_old_pipeline_runs(tmp_path):
    reg = Registry(tmp_path / "registry")
    s = Settings(registry=reg.root, pmaports=PMAPORTS, out=tmp_path / "out", work=tmp_path / "work", dry_run=True)
    run = Runner(dry_run=True, log=lambda _: None)
    build(resolve("oneplus-enchilada", reg, Pmaports(PMAPORTS)), s, reg, run)
    flat = [" ".join(c) for c in run.commands]
    # the old pipeline installs ztOSIAL too (from --ztos-src), but runs none of
    # the GSI's steps, such as checking the assembled root against it
    assert any("pacstrap" in c for c in flat) and not any("ztos-ial check" in c for c in flat)


def test_gsi_dry_run_through_the_cli(tmp_path, capsys):
    src = make_gsi(tmp_path / "dl")
    assert main(["--registry", str(tmp_path / "reg"), "--pmaports", str(PMAPORTS), "--dry-run",
                 "build", "oneplus-enchilada", "--out", str(tmp_path / "out"), "--gsi", str(src)]) == 0
    assert "ztos-ial" in capsys.readouterr().err


def native_registry(tmp_path) -> Registry:
    reg = Registry(tmp_path / "reg")
    data = entry(source={"native": {"repo": "https://example.org/d", "commit": "c"}})
    data["device"]["origin"] = "native"
    reg.write(data)
    return reg


def test_native_takes_a_device_layer_tarball(tmp_path, capsys):
    reg = native_registry(tmp_path)
    layer = tmp_path / "layer.tar.zst"
    code = main(["--registry", str(reg.root), "--pmaports", str(PMAPORTS), "--dry-run", "build", "vendor-phone",
                 "--gsi", str(make_gsi(tmp_path / "dl")), "--device-layer", str(layer)])
    err = capsys.readouterr().err
    assert code == 0 and f"device layer: device-layer tarball {layer}" in err


def test_native_takes_the_vendor_package_from_a_repo(tmp_path, capsys):
    reg = native_registry(tmp_path)
    assert main(["--registry", str(reg.root), "--pmaports", str(PMAPORTS), "--dry-run", "build", "vendor-phone",
                 "--gsi", str(make_gsi(tmp_path / "dl")), "--ztos-repo", "https://repo.example/$arch"]) == 0
    err = capsys.readouterr().err
    assert "ztos-kernel-vendor-phone ztos-vendor-vendor-phone" in err


def test_native_with_no_device_layer_stops(tmp_path, monkeypatch):
    monkeypatch.delenv("ZTCONFLO_ZTOS_REPO", raising=False)
    reg = native_registry(tmp_path)
    s = Settings(registry=reg.root, out=tmp_path / "out", work=tmp_path / "work", dry_run=True,
                 gsi=str(make_gsi(tmp_path / "dl")), gsi_cache=tmp_path / "cache", ztos_repo="")
    with pytest.raises(Exit) as err:
        build(resolve("vendor-phone", reg, Pmaports(PMAPORTS)), s, reg, Runner(dry_run=True, log=lambda _: None))
    assert err.value.code == Exit.BUILD_FAILED and "--device-layer" in str(err.value)


class FailingRunner(Runner):
    def run(self, cmd, **kw):
        super().run(cmd, **kw)
        return subprocess.CompletedProcess(cmd, 1, "", "")


def test_a_failed_ial_check_is_exit_3(tmp_path):
    with pytest.raises(Exit) as err:
        ial_check(FailingRunner(dry_run=True, log=lambda _: None), tmp_path)
    assert err.value.code == Exit.BUILD_FAILED and "ztos-ial check failed" in str(err.value)
    said = []
    ial_check(Runner(log=said.append), tmp_path)                    # a GSI without ztos-ial: skipped
    assert "skipped" in said[0]


def test_lookup_prints_the_contract_from_an_earlier_build(tmp_path, capsys, monkeypatch):
    import ztconflo.cli as cli
    work = tmp_path / "work"
    (work / "oneplus-enchilada/kernel/boot").mkdir(parents=True)
    (work / "oneplus-enchilada/kernel/boot/config-6.18").write_text((KCONFIG / "no-binder.config").read_text())
    orig = cli._settings
    monkeypatch.setattr(cli, "_settings", lambda args: Settings(**{**vars(orig(args)), "work": work}))
    assert main(["--registry", str(tmp_path / "reg"), "--pmaports", str(PMAPORTS), "lookup", "oneplus-enchilada"]) == 0
    out = capsys.readouterr().out
    assert "contract: met, with gaps" in out and "Waydroid hidden" in out
