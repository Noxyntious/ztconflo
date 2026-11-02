# SPDX-License-Identifier: GPL-2.0-or-later
"""The registry file format, status rules, and flash.sh for each method."""
import tomllib

import pytest

from ztconflo import Exit
from ztconflo.flash import flash, script, targets
from ztconflo.registry import Registry, advance, dumps, validate


def entry(method="fastboot", **boot):
    return {
        "device": {"codename": "vendor-phone", "name": "Vendor Phone", "arch": "aarch64",
                   "status": "untested", "origin": "pmos"},
        "source": {"pmos": {"pmaports_commit": "abc", "device_pkg": "device-vendor-phone",
                            "kernel_pkg": "linux-x", "firmware_pkgs": ["firmware-x"], "category": "testing"}},
        "kernel": {"mainline": True, "version": "6.18", "artifacts_sha256": ""},
        "boot": {"flash_method": method, "rootfs_target": "userdata", "cmdline": 'a="b" c', **boot},
        "userspace": {"daemons": ["rmtfs"]},
        "ztmetro": {"scale": 2.5, "vibrator": "auto", "rotation": 0},
        "verified": {"date": "", "by": "", "checklist": {}},
    }


def test_toml_round_trip():
    data = entry(defaulted=["pagesize"], pagesize=4096, append_dtb=True)
    data["verified"]["checklist"] = {"touch": "pass", "modem": "absent"}
    back = tomllib.loads(dumps(data))
    assert back == data


def test_write_and_find(tmp_path):
    reg = Registry(tmp_path)
    reg.write(entry())
    assert reg.find("vendor-phone").codename == "vendor-phone"
    assert reg.find("phone").codename == "vendor-phone"
    assert reg.find("other") is None
    assert [e.codename for e in reg.entries()] == ["vendor-phone"]


def test_validation_rejects_bad_entries():
    bad = entry()
    bad["device"]["status"] = "great"
    with pytest.raises(ValueError):
        validate(bad)
    native = entry()
    native["device"]["origin"] = "native"
    with pytest.raises(ValueError):
        validate(native)          # native must not carry [source.pmos]


def test_status_only_moves_forward():
    assert advance("untested", "boots") == "boots"
    assert advance("tested", "boots") == "tested"
    assert advance("boots", "untested") == "boots"


def test_fastboot_script():
    s = script(entry())
    assert "fastboot flash boot boot.img" in s and "fastboot flash userdata rootfs.img" in s
    assert s.startswith("#!/bin/sh")


def test_heimdall_scripts():
    s = script(entry("heimdall-bootimg", heimdall_partition_kernel="BOOT", rootfs_target="USERDATA"))
    assert "heimdall flash --BOOT boot.img --USERDATA rootfs.img" in s
    iso = entry("heimdall-isorec", heimdall_partition_kernel="KERNEL", heimdall_partition_initfs="RECOVERY",
                rootfs_target="SYSTEM")
    assert [p for _, p in targets(iso["boot"])] == ["KERNEL", "RECOVERY", "SYSTEM"]


def test_unsupported_method():
    with pytest.raises(Exit):
        script(entry("mtkclient"))


def test_flash_needs_the_codename_typed(tmp_path, capsys):
    (tmp_path / "flash.sh").write_text("#!/bin/sh\n")
    ran = []
    flash(tmp_path, entry(), ask=lambda _: "no", run=lambda *a, **k: ran.append(a))
    assert ran == [] and "Not flashed" in capsys.readouterr().out
    flash(tmp_path, entry(), ask=lambda _: "vendor-phone", run=lambda *a, **k: ran.append(a))
    assert len(ran) == 1


def test_lk2nd_phones_with_fastboot_only_write_userdata():
    # a Xiaomi has fastboot of its own: lk2nd is put on BOOT once by hand and
    # stays; flash.sh must not fall back to Samsung's heimdall or write BOOT
    s = script(entry("fastboot", lk2nd="lk2nd-msm8953"))
    assert "heimdall" not in s
    assert "lk2nd's fastboot" in s
    assert "fastboot flash userdata rootfs.img" in s
    assert "fastboot flash boot" not in s.replace("fastboot flash boot lk2nd.img", "")
    assert "Overwrites: userdata." in s


def test_samsung_lk2nd_phones_keep_heimdall():
    data = entry("fastboot", lk2nd="lk2nd-msm8916")
    data["device"]["name"] = "Samsung Galaxy J5"
    s = script(data)
    assert "heimdall flash --BOOT lk2nd.img" in s
    assert "Overwrites: BOOT (lk2nd), userdata." in s
