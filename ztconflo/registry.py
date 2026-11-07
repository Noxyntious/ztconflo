# SPDX-License-Identifier: GPL-2.0-or-later
"""The ztOS device registry: one folder per phone, devices/<codename>/device.toml.

Read with tomllib; written with a small emitter for this one fixed schema, so
ztConflo needs nothing outside the standard library."""
import re
import subprocess
import tomllib
from pathlib import Path

from . import Exit

STATUSES = ("untested", "boots", "tested")
ORIGINS = ("pmos", "native")
SECTIONS = ("device", "source.pmos", "source.native", "kernel", "boot", "userspace", "ztmetro", "ial", "android",
            "verified")
BINDERS = ("binderfs", "none")
BACKENDS = ("generic", "vendor", "service", "none")
_INTERFACE = re.compile(r"^[A-Z][A-Za-z]*[0-9]+$")        # Display1, CallAudio1
_BUILD_DIR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # userspace/<name>, no paths
REQUIRED = {
    "device": ("codename", "name", "arch", "status", "origin"),
    "boot": ("flash_method", "rootfs_target"),
}


class Entry(dict):
    """A device.toml as nested dicts, plus where it lives."""

    def __init__(self, data: dict, path: Path | None = None):
        super().__init__(data)
        self.path = path

    @property
    def codename(self) -> str:
        return self["device"]["codename"]

    @property
    def status(self) -> str:
        return self["device"]["status"]

    @property
    def origin(self) -> str:
        return self["device"]["origin"]

    @property
    def folder(self) -> Path | None:
        return self.path.parent if self.path else None


def validate(data: dict, where: str = "device.toml") -> None:
    for section, keys in REQUIRED.items():
        if section not in data:
            raise ValueError(f"{where}: missing [{section}]")
        for key in keys:
            if key not in data[section]:
                raise ValueError(f"{where}: missing [{section}].{key}")
    dev = data["device"]
    if dev["status"] not in STATUSES:
        raise ValueError(f"{where}: status must be one of {', '.join(STATUSES)}")
    if dev["origin"] not in ORIGINS:
        raise ValueError(f"{where}: origin must be one of {', '.join(ORIGINS)}")
    has_pmos = "pmos" in data.get("source", {})
    if dev["origin"] == "pmos" and not has_pmos:
        raise ValueError(f"{where}: origin = \"pmos\" needs [source.pmos]")
    if dev["origin"] == "native" and has_pmos:
        raise ValueError(f"{where}: origin = \"native\" must not have [source.pmos]")
    _validate_ial(data, where)
    _validate_userspace(data, where)


def _validate_userspace(data: dict, where: str) -> None:
    """[userspace].build: folders of the entry's userspace/ that the build makes
    in the root (make, make install), with build_deps from pacman first."""
    us = data.get("userspace", {})
    for key in ("daemons", "build", "build_deps"):
        v = us.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise ValueError(f"{where}: [userspace].{key} must be a list of strings")
    for name in us.get("build", []):
        if not _BUILD_DIR.match(name):
            raise ValueError(f"{where}: [userspace].build: '{name}' must be a folder name in userspace/")


def _validate_ial(data: dict, where: str) -> None:
    """The ztOSIAL tables, all optional so older entries stay valid."""
    native = data.get("source", {}).get("native")
    if native is not None:
        if data["device"]["origin"] != "native":
            raise ValueError(f"{where}: [source.native] is only for origin = \"native\"")
        for key in ("repo", "commit"):
            if not isinstance(native.get(key), str):
                raise ValueError(f"{where}: [source.native].{key} must be a string")
    for name, table in data.get("ial", {}).items():
        if not _INTERFACE.match(name):
            raise ValueError(f"{where}: [ial].{name} is not an interface name (like Display1)")
        if not isinstance(table, dict):
            raise ValueError(f"{where}: [ial].{name} must be a table, like {{ node = \"...\" }}")
        if table.get("backend", "generic") not in BACKENDS:
            raise ValueError(f"{where}: [ial].{name}.backend must be one of {', '.join(BACKENDS)}")
        # as ztos-ial check: a vendor backend is a Python module in the device
        # layer, a service backend is its own process owning org.ztos.ial.<Area>
        if table.get("backend") == "vendor" and not table.get("module"):
            raise ValueError(f"{where}: [ial].{name} has a vendor backend, so it needs a module")
        if table.get("backend") == "service" and not table.get("service"):
            raise ValueError(f"{where}: [ial].{name} has a service backend, so it needs a service")
    android = data.get("android", {})
    if "binder" in android and android["binder"] not in BINDERS:
        raise ValueError(f"{where}: [android].binder must be one of {', '.join(BINDERS)}")
    if "gralloc" in android and not isinstance(android["gralloc"], str):
        raise ValueError(f"{where}: [android].gralloc must be a string")


def load(path: Path) -> Entry:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    validate(data, str(path))
    return Entry(data, path)


class Registry:
    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def devices(self) -> Path:
        return self.root / "devices"

    def entries(self) -> list[Entry]:
        if not self.devices.is_dir():
            return []
        return [load(p) for p in sorted(self.devices.glob("*/device.toml"))]

    def find(self, codename: str) -> Entry | None:
        """An exact codename, or a short one (enchilada for oneplus-enchilada)
        when exactly one entry ends with it."""
        exact = self.devices / codename / "device.toml"
        if exact.is_file():
            return load(exact)
        matches = [e for e in self.entries() if e.codename.endswith("-" + codename)]
        if len(matches) > 1:
            names = ", ".join(e.codename for e in matches)
            raise Exit(Exit.NOT_FOUND, f"'{codename}' is ambiguous in the registry: {names}")
        return matches[0] if matches else None

    def write(self, entry: dict) -> Path:
        validate(entry)
        folder = self.devices / entry["device"]["codename"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "device.toml"
        path.write_text(dumps(entry))
        return path

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root), *args], check=check,
                              capture_output=True, text=True)


def advance(current: str, new: str) -> str:
    """Status only moves forward: untested -> boots -> tested."""
    if STATUSES.index(new) < STATUSES.index(current):
        return current
    return new


# ---- a TOML emitter for this schema ------------------------------------------------------

def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + "}"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else _value(k)


def dumps(data: dict) -> str:
    """Sections in the schema's order, then anything else, each key = value."""
    out = []
    order = [s for s in SECTIONS] + [k for k in data if k not in ("device", "source", "kernel", "boot",
                                                                   "userspace", "ztmetro", "ial", "android",
                                                                   "verified")]
    for name in order:
        if name.startswith("source."):
            table = data.get("source", {}).get(name.split(".", 1)[1])
        else:
            table = data.get(name)
        if table is None:
            continue
        out.append(f"[{name}]")
        for k, v in table.items():
            out.append(f"{_key(k)} = {_value(v)}")
        out.append("")
    return "\n".join(out)
