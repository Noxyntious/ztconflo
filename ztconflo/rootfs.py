# SPDX-License-Identifier: GPL-2.0-or-later
"""The root filesystem: Arch Linux ARM by pacstrap, the kernel's modules and
firmware, ztMETRO built from the ztOSMobile sources, then configuration.

Runs on an x86_64 Arch host; aarch64 programs in the new root run through
qemu-user (binfmt), so `arch-chroot` works as on the phone."""
import shlex
import tarfile
import urllib.request
from pathlib import Path

from . import Exit
from .config import ALARM_MIRROR, CACHE
from .runner import Runner

KEYRING_URL = "http://mirror.archlinuxarm.org/aarch64/core/"
DEFAULT_USER = "alarm"        # what ztOSMobile's session scripts and sudoers expect

BASE = ["base", "systemd", "archlinuxarm-keyring", "sudo", "openssh", "networkmanager", "greetd",
        "mesa", "vulkan-icd-loader", "pipewire", "pipewire-pulse", "wireplumber", "modemmanager",
        "python-gobject"]          # ztos-iald (ztOSIAL) is Python over Gio
# what ztmetro/Makefile and the SDK need to build and run
BUILD_DEPS = ["base-devel", "wlroots0.20", "wayland", "wayland-protocols", "libxkbcommon", "cairo", "pango",
              "librsvg", "pixman", "libdrm", "libpulse", "gdk-pixbuf2", "systemd-libs"]
SDK_DEPS = ["python", "pyside6", "qt6-svg", "qt6-wayland"]

# ztOSMobile's rootfs/overlay is the S9's; only these parts are every phone's
GENERIC_OVERLAY = [
    "etc/greetd/config.toml",
    "etc/sudoers.d/10-wheel",
    "etc/sudoers.d/99-ztos-alarm",
    "etc/systemd/network/10-zboot-usb.network",
    "etc/systemd/network/20-wlan.network",
    "etc/systemd/system/zboot-usb-gadget.service",
    # ztMETRO takes the power key: without this logind powers the phone off on a short press
    "etc/systemd/logind.conf.d/10-zboot.conf",
    "etc/tmpfiles.d/99-ztos-compat.conf",
    # the kernel log of a crash or hang, kept per boot (systemd-pstore, enabled in configure)
    "etc/systemd/pstore.conf.d/ztos.conf",
    "etc/systemd/system/ztos-pstore-keep.service",
    "usr/local/sbin/zboot-session",
    "usr/local/sbin/zboot-usb-gadget",
    "usr/local/bin/zboot-osk",
]


def pacman_conf(work: Path) -> Path:
    """An aarch64 pacman.conf for pacstrap on the x86_64 host, with its own
    keyring so signatures are checked against Arch Linux ARM's keys. The same
    repos as Arch Linux ARM's own pacman.conf, aur too: pacstrap syncs only
    these, and pacman -S in the root stops at a repo it has no database for."""
    conf = work / "pacman-aarch64.conf"
    conf.write_text(f"""[options]
Architecture = aarch64
GPGDir = {work / 'gnupg'}
SigLevel = Required DatabaseOptional
LocalFileSigLevel = Optional
ParallelDownloads = 5
DisableSandbox

[core]
Server = {ALARM_MIRROR}
[extra]
Server = {ALARM_MIRROR}
[alarm]
Server = {ALARM_MIRROR}
[aur]
Server = {ALARM_MIRROR}
""")
    return conf


def keyring(run: Runner, work: Path) -> None:
    """Arch Linux ARM's keys, from its own keyring package, into work/gnupg."""
    gnupg = work / "gnupg"
    if (gnupg / "pubring.gpg").exists() or (gnupg / "pubring.kbx").exists():
        return
    keys = CACHE / "alarm-keyring"
    if not run.dry_run:
        keys.mkdir(parents=True, exist_ok=True)
        pkg = _fetch_keyring(keys)
        with tarfile.open(pkg) as tar:
            tar.extractall(keys, members=[m for m in tar.getmembers() if "keyrings/" in m.name], filter="tar")
    run.run(["pacman-key", "--gpgdir", gnupg, "--init"], root=True)
    run.run(["pacman-key", "--gpgdir", gnupg, "--populate-from", keys / "usr/share/pacman/keyrings",
             "--populate", "archlinuxarm"], root=True)


def _fetch_keyring(dest: Path) -> Path:
    import re
    index = urllib.request.urlopen(KEYRING_URL, timeout=60).read().decode()
    names = sorted(set(re.findall(r'href="(archlinuxarm-keyring-[^"]+\.pkg\.tar\.(?:xz|zst))"', index)))
    if not names:
        raise Exit(Exit.BUILD_FAILED, f"archlinuxarm-keyring not found at {KEYRING_URL}")
    out = dest / names[-1]
    if not out.exists():
        urllib.request.urlretrieve(KEYRING_URL + names[-1], out)
    if out.suffix == ".zst":
        raise Exit(Exit.BUILD_FAILED, f"{out.name} is zstd; extract it with bsdtar first")
    return out


def pacstrap(run: Runner, root: Path, conf: Path, extra: list[str], jobs: int) -> None:
    packages = list(dict.fromkeys(BASE + BUILD_DEPS + SDK_DEPS + extra))     # once each, in order
    run.run(["mkdir", "-p", root], root=True)
    run.run(["pacstrap", "-C", conf, "-G", "-M", root, *packages], root=True)


def kernel_side(run: Runner, root: Path, kernel_tree: Path) -> None:
    """/lib/modules and /lib/firmware from the unpacked postmarketOS packages,
    which put them under /lib or, lately, /usr/lib."""
    for sub in ("lib/modules", "lib/firmware"):
        for src in (kernel_tree / sub, kernel_tree / "usr" / sub):
            if run.dry_run or src.is_dir():
                run.run(["mkdir", "-p", root / "usr" / sub], root=True)
                run.run(["cp", "-a", f"{src}/.", root / "usr" / sub], root=True)


def landlock_missing(*trees: Path) -> bool | None:
    """Whether the kernel in these trees lacks Landlock; None when its config
    cannot be found."""
    from . import contract
    path = contract.find_config(*trees)
    if path is None:
        return None
    return contract.read_config(path).get("SECURITY_LANDLOCK", "n") in ("n", "")


def pacman_sandbox(run: Runner, root: Path, *trees: Path) -> None:
    """pacman 7 downloads as alpm inside a Landlock sandbox and, on a kernel
    without Landlock (pmOS's msm8953, for one), downloads nothing at all. Only
    the filesystem part goes then (DisableSandboxFilesystem, pacman 7.1): the
    alpm user and the syscall filter stay."""
    missing = landlock_missing(*trees) if not run.dry_run else True
    if missing is None:
        run.log("    no kernel config found: pacman's download sandbox left as it is")
        return
    if not missing:
        return
    run.run(["sed", "-i", "-e", "/^#*DisableSandboxFilesystem/d", "-e",
             r"/^\[options\]/a # ztConflo: this kernel has no Landlock, which pacman's download sandbox needs"
             r"\nDisableSandboxFilesystem", root / "etc/pacman.conf"], root=True)


def chroot(run: Runner, root: Path, script: str) -> None:
    run.run(["arch-chroot", root, "/bin/bash", "-euc", script], root=True)


def ztmetro_from_source(run: Runner, root: Path, src: Path, jobs: int) -> None:
    """The step the spec's pacman packages would do: copy ztOSMobile's ztMETRO,
    SDK and system apps into the root and build them there, under qemu."""
    if not run.dry_run and not (src / "ztmetro/Makefile").is_file():
        raise Exit(Exit.BUILD_FAILED, f"no ztOSMobile checkout at {src} (use --ztos-src)")
    dest = root / "usr/src/ztos"
    run.run(["mkdir", "-p", dest], root=True)
    # ial/: ztOSIAL, built first so ztmetro/Makefile finds ztos-ial.pc (HAVE_ZTOS_IAL)
    parts = ["ial"] if run.dry_run or (src / "ial/Makefile").is_file() else []
    # apps/: the apps ztOS ships besides its own (Music)
    extra = ["apps"] if run.dry_run or (src / "apps").is_dir() else []
    # android/push: zrn-pushd, push messages for Android apps (installer_parts)
    extra += ["android/push"] if run.dry_run or (src / "android/push/Makefile").is_file() else []
    for part in (*parts, "ztmetro", "sdk", "os", *extra):
        if "/" in part:
            run.run(["mkdir", "-p", (dest / part).parent], root=True)
        run.run(["rsync", "-a", "--delete", "--exclude", "ref", "--exclude", "__pycache__", "--exclude", "*.o",
                 f"{src / part}/", f"{dest / part}/"], root=True)
    chroot(run, root, f"""
if [ -f /usr/src/ztos/ial/Makefile ]; then
	cd /usr/src/ztos/ial
	make clean >/dev/null 2>&1 || true
	make
	make install
fi
cd /usr/src/ztos/ztmetro
make clean >/dev/null 2>&1 || true
make -j{jobs}
make install
# the SDK runtime and its system apps, where the phone's launcher looks for them
install -d /usr/lib/ztos /usr/local/lib/ztos/apps
cp -a /usr/src/ztos/sdk/runtime/. /usr/lib/ztos/
""")


# ztOS's own apps, packed by its SDK and installed as ztOSMobile's installer does
# (the default tiles start Welcome and Updates); built in the root, so the SDK's
# GLES renderer is built for it there
SYSTEM_APPS = ("os/apps/welcome", "os/apps/updates", "apps/music")
BUILD_APPS = r"""
import shutil, sys
from pathlib import Path
sys.path.insert(0, "/usr/src/ztos/sdk")
from ztos_sdk import cli
python = "%d.%d" % sys.version_info[:2]
for folder in sys.argv[1:]:
    project = Path("/usr/src/ztos") / folder
    if not (project / "ztos.toml").is_file():
        print(f"note: no {folder} in this checkout"); continue
    p = cli.find_project(str(project))
    shutil.copy2(cli.build(p, "aarch64", python=python), f"/usr/local/lib/ztos/apps/{p.id}")
    shutil.copy2(project / p.icon, f"/usr/local/share/icons/hicolor/scalable/apps/{p.id}.svg")
    Path(f"/usr/local/share/applications/{p.id}.desktop").write_text("\n".join([
        "[Desktop Entry]", "Type=Application", f"Name={p.name}",
        f"Comment={p.manifest['description'] or p.name}",
        f"Exec=/usr/local/lib/ztos/apps/{p.id}",
        f"Icon=/usr/local/share/icons/hicolor/scalable/apps/{p.id}.svg",
        "Terminal=false", f"StartupWMClass=ztos.{p.id}", "Categories=System;", "",
    ]))
    print(f"app: {p.id}")
"""


def system_apps(run: Runner, root: Path) -> None:
    chroot(run, root, "install -d /usr/local/lib/ztos/apps /usr/local/share/applications "
                      "/usr/local/share/icons/hicolor/scalable/apps\n"
                      "cd /usr/src/ztos\n"
                      f"python3 - {' '.join(SYSTEM_APPS)} <<'PY'\n{BUILD_APPS}PY\n")


# The rest of what ztOSMobile's installer puts on every phone
# (installer/ztos_installer/build.py), from the checkout copied into the root;
# each part only when the checkout has it
INSTALLER_PARTS = r"""
cd /usr/src/ztos
# the browser (Superscan, on WPE WebKit)
make -C ztmetro install-browser
# zrn-pushd: push messages for Android apps, a user service in every session
if [ -f android/push/Makefile ]; then
	make -C android/push clean >/dev/null 2>&1 || true
	make -C android/push
	make -C android/push install
	install -Dm644 android/push/README.md /usr/local/share/doc/zrn-pushd/README.md
	systemctl --global enable zrn-pushd.service
fi
# Selawik, the fonts ztMETRO's text falls back to
if [ -d os/fonts/selawik ]; then
	install -d /usr/local/share/fonts/selawik
	install -m644 os/fonts/selawik/*.ttf /usr/local/share/fonts/selawik/
	fc-cache -f >/dev/null 2>&1 || true
fi
# the SDK, to build ztOS apps on the phone, and the runtime each user's
# apps start from (skel: before configure makes the user)
rm -rf /usr/local/lib/ztos-sdk
cp -a sdk /usr/local/lib/ztos-sdk
ln -sf ../lib/ztos-sdk/bin/ztos-sdk /usr/local/bin/ztos-sdk
install -Dm644 sdk/runtime/zygote.py /etc/skel/.local/share/ztos/zygote.py
install -d /etc/skel/.local/share/ztos/lib/ztos
install -m644 sdk/runtime/ztos/*.py /etc/skel/.local/share/ztos/lib/ztos/
python3 -m compileall -q /etc/skel/.local/share/ztos/lib >/dev/null || true
"""


def installer_parts(run: Runner, root: Path) -> None:
    chroot(run, root, INSTALLER_PARTS)


def media(run: Runner, root: Path, src: Path) -> None:
    """ztOS's wallpapers and ringtones: prebuilts/ztos-media, split in parts
    (ztOSMobile's scripts/pack-media.sh), unpacked as its installer does."""
    parts = sorted((src / "prebuilts/ztos-media").glob("ztos-media.tar.xz.part*"))
    if not run.dry_run and not parts:
        run.log("    no prebuilts/ztos-media in the ztOSMobile checkout: no wallpapers or ringtones")
        return
    glob = shlex.quote(str(src / "prebuilts/ztos-media")) + "/ztos-media.tar.xz.part*"
    run.run(["sh", "-c", f"cat {glob} | tar -C {shlex.quote(str(root))} --no-same-owner -xJf -"], root=True)


def android_runtime(run: Runner, root: Path, src: Path) -> None:
    """ztOSAndroidRuntimeV1 (ATL, ART, bionic_translation: Android apps as
    Linux processes, without binder), as ztOSMobile's installer ships it: its
    own android/atl/install-runtime.sh checks the bundle's hashes and CPU and
    installs the Arch packages it needs. The bundle stays on the phone, where
    ztos-android-dependencies installs it again."""
    packages = src / "prebuilts/ztOSAndroidRuntimeV1/packages"
    script = src / "android/atl/install-runtime.sh"
    if not run.dry_run and not ((packages / "SHA256SUMS").is_file() and script.is_file()):
        run.log("    no prebuilts/ztOSAndroidRuntimeV1 in the ztOSMobile checkout: Android apps (ATL) left out")
        return
    keep = root / "usr/local/share/ztos/android-runtime"
    run.run(["mkdir", "-p", keep], root=True)
    run.run(["rsync", "-a", "--delete", "--chown=0:0", f"{packages}/", f"{keep / 'packages'}/"], root=True)
    run.run(["install", "-m", "755", script, keep / "install-runtime.sh"], root=True)
    # pacman's free-space check needs the root to be a mount point, which the
    # build's root is not ("could not determine cachedir mount point"): off for
    # the install only, and back on however it ends
    # pacman checks the packages it downloads: the root's keyring, which
    # configure (step 4, after this) would otherwise be first to set up
    chroot(run, root, "[ -s /etc/pacman.d/gnupg/pubring.gpg ] || "
                      "{ pacman-key --init && pacman-key --populate archlinuxarm; } >/dev/null\n"
                      "sed -i 's/^CheckSpace/#CheckSpace/' /etc/pacman.conf\n"
                      "trap \"sed -i 's/^#CheckSpace/CheckSpace/' /etc/pacman.conf\" EXIT\n"
                      "bash /usr/local/share/ztos/android-runtime/install-runtime.sh "
                      "/usr/local/share/ztos/android-runtime/packages")


def overlay(run: Runner, root: Path, src: Path, device_overlay: Path | None) -> None:
    """The generic ztOS files, then the phone's own overlay/ last (it wins)."""
    # os/overlay: what makes the system ztOS on every phone (BETA-1 on): the user
    # templates and ztos-user-dropins (greetd as the phone's user), fonts, skel,
    # updater, store. Before configure, so useradd copies skel and the dropins run
    system = src / "os/overlay"
    if run.dry_run or system.is_dir():
        run.run(["cp", "-a", "--no-preserve=ownership", f"{system}/.", root], root=True)
    for rel in GENERIC_OVERLAY:
        f = src / "rootfs/overlay" / rel
        if run.dry_run or f.exists():
            run.run(["install", "-D", "-m", "755" if "/bin/" in rel or "/sbin/" in rel else "644",
                     f, root / rel], root=True)
    media(run, root, src)
    if device_overlay is not None and (run.dry_run or device_overlay.is_dir()):
        run.run(["cp", "-a", "--no-preserve=ownership", f"{device_overlay}/.", root], root=True)


def configure(run: Runner, root: Path, entry: dict, ssh_key: Path | None) -> None:
    codename = entry["device"]["codename"]
    zt = entry.get("ztmetro", {})
    daemons = entry.get("userspace", {}).get("daemons", [])
    config = f"scale = {zt.get('scale', 2.5)}\\nvibrator = {zt.get('vibrator', 'auto')}\\nrotation = {zt.get('rotation', 0)}\\n"
    key = ssh_key.read_text().strip() if ssh_key and ssh_key.is_file() else ""
    chroot(run, root, f"""
echo ztos-{codename} > /etc/hostname
pacman-key --init >/dev/null 2>&1 && pacman-key --populate archlinuxarm >/dev/null 2>&1 || true
id {DEFAULT_USER} >/dev/null 2>&1 || useradd -m -G wheel,video,input,audio -s /bin/bash {DEFAULT_USER}
echo '{DEFAULT_USER}:{DEFAULT_USER}' | chpasswd
# ztOS (BETA-1 on) learns its user from /etc/ztos/user.conf; ztos-user-dropins
# then writes what cannot read it (greetd, sudoers, the user manager's limits)
install -d /etc/ztos
printf 'ZTOS_USER=%s\nZTOS_UID=%s\nZTOS_GID=%s\nZTOS_HOME=%s\n' {DEFAULT_USER} \
	"$(id -u {DEFAULT_USER})" "$(id -g {DEFAULT_USER})" "$(getent passwd {DEFAULT_USER} | cut -d: -f6)" > /etc/ztos/user.conf
[ -x /usr/local/sbin/ztos-user-dropins ] && /usr/local/sbin/ztos-user-dropins >/dev/null || true
install -d -m 755 /etc/ztmetro
printf '{config}' > /etc/ztmetro/config
# no scale for ztmetro: it sizes itself to the panel (360 points across), and
# a leftover ZTMETRO_SCALE would pin it
[ -f /etc/ztos/session.env ] && sed -i '/ZTMETRO_SCALE/d' /etc/ztos/session.env
systemctl enable greetd NetworkManager sshd systemd-networkd >/dev/null
# modules the device overlay brought (extra/, updates/) load by their aliases
for k in /usr/lib/modules/*/; do depmod -a "$(basename "$k")"; done
systemctl enable ModemManager >/dev/null 2>&1 || echo "note: no unit ModemManager in this root"
systemctl enable zboot-usb-gadget >/dev/null 2>&1 || true
systemctl enable ztos-iald >/dev/null 2>&1 || echo "note: no unit ztos-iald in this root"
# ztMETRO's clocks: without it the GPU rests at its lowest step and every animation lags
systemctl enable zrnperfd >/dev/null 2>&1 || echo "note: no unit zrnperfd in this root"
# the rest of the services ztOSMobile's installer turns on: names through resolved
# (NetworkManager hands it each network's DNS), crash logs kept, the sensor
# framework, the update check
for u in systemd-resolved systemd-pstore ztos-pstore-keep ztoscoresensorframework ztos-update-check.timer; do
	systemctl enable "$u" >/dev/null 2>&1 || echo "note: no unit $u in this root"
done
update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
{''.join(f'systemctl enable {d} >/dev/null 2>&1 || echo "note: no unit {d} in this root"' + chr(10) for d in daemons)}
if [ -x /usr/local/sbin/zboot-session ]; then
	sed -i "s|^command = .*|command = \\"/usr/local/bin/ztmetro-session\\"|" /etc/greetd/config.toml
fi
if [ -n '{key}' ]; then
	install -d -m 700 -o {DEFAULT_USER} -g {DEFAULT_USER} /home/{DEFAULT_USER}/.ssh
	echo '{key}' > /home/{DEFAULT_USER}/.ssh/authorized_keys
	chown {DEFAULT_USER}: /home/{DEFAULT_USER}/.ssh/authorized_keys; chmod 600 /home/{DEFAULT_USER}/.ssh/authorized_keys
fi
""")
    # resolved's stub as resolv.conf, as the installer leaves it; from outside,
    # because arch-chroot mounts the host's resolv.conf over it inside
    run.run(["ln", "-sfn", "/run/systemd/resolve/stub-resolv.conf", root / "etc/resolv.conf"], root=True)


def package_list(f: Path | None) -> list[str]:
    """Package names, one or more per line, '#' comments."""
    if not f or not f.is_file():
        return []
    return [w for line in f.read_text().splitlines() for w in line.split("#")[0].split()]


def packages_txt(folder: Path | None) -> list[str]:
    return package_list(folder / "packages.txt" if folder else None)


def os_packages(src: Path) -> list[str]:
    """What every ztOS system carries (os/packages/common, as ztOSMobile's
    installer installs it): the terminal, brightnessctl, the browser, Waydroid..."""
    return package_list(src / "os/packages/common")
