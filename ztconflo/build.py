# SPDX-License-Identifier: GPL-2.0-or-later
"""The pipeline: a resolved entry in, out/<codename>/ with boot.img, rootfs.img,
build.json and flash.sh out."""
import json
import shutil
import time
from pathlib import Path

from . import Exit, contract, flash, gsi, ial, images, pmos, rootfs
from .config import PMAPORTS_URL, Settings
from .registry import Entry, Registry
from .resolve import Resolution, draft_entry, entry_warnings
from .runner import Runner


def entry_for(res: Resolution, settings: Settings, registry: Registry, run: Runner) -> Entry:
    """The registry's entry, or a fresh draft one from pmaports, written to the
    registry as untested (the spec: every fallback build leaves an entry)."""
    if res.source == "registry":
        return res.entry
    pm = pmos.Pmaports(settings.pmaports)
    commit = pm.commit() if pm.exists() else "unknown"
    data = draft_entry(res.device, res.verdict, commit, pm.kernel_version(res.verdict.kernel_pkg))
    if run.dry_run:
        run.log(f"    (would write draft entry {registry.devices / res.device.codename / 'device.toml'})")
        return Entry(data)
    path = registry.write(data)
    run.log(f"    draft registry entry: {path}")
    return Entry(data, path)


def _pmbootstrap_config(work: Path, aports: Path, codename: str) -> Path:
    cfg = work / "pmbootstrap.cfg"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f"[pmbootstrap]\naports = {aports}\ndevice = {codename}\nwork = {work}\n"
                   "ui = none\nuser = user\nkernel = \nis_default_channel = False\n")
    # what `pmbootstrap init` would have written; we never run init
    version = work / "version"
    if not version.is_file():
        version.write_text(pmos.work_version() + "\n")
    (work / "cache_git").mkdir(exist_ok=True)
    return cfg


def pmbootstrap_device(entry: Entry) -> str:
    """The pmaports device pmbootstrap installs: the entry's device package, so a
    phone built from a generic port (xiaomi-daisy from device-qcom-msm8953) gets
    that port, else the codename."""
    pkg = entry.get("source", {}).get("pmos", {}).get("device_pkg", "")
    return pkg.removeprefix("device-") or entry.codename


def fetch_kernel(run: Runner, entry: Entry, settings: Settings, tree: Path, step: str = "1. kernel side") -> dict:
    """Step 1: kernel, modules, firmware and initramfs, unpacked under `tree`."""
    run.step(step)
    if entry.origin == "native":
        raise Exit(Exit.BUILD_FAILED, "origin = \"native\" needs the ztOS pacman repository, which does not exist "
                                      "yet; this entry cannot be built until it does")
    src = entry["source"]["pmos"]
    pm = pmos.Pmaports(settings.pmaports)
    pm.ensure(PMAPORTS_URL, run.dry_run)
    if not run.dry_run and src["pmaports_commit"] not in ("", "unknown") and pm.commit() != src["pmaports_commit"]:
        pm.checkout(src["pmaports_commit"])
    work = settings.pmbootstrap_work
    pmb_device = pmbootstrap_device(entry)
    cfg = _pmbootstrap_config(work, settings.pmaports, pmb_device) if not run.dry_run else work / "pmbootstrap.cfg"
    base = ["pmbootstrap", "-c", cfg, "--work", work, "--aports", settings.pmaports, "-y"]
    pkgs = [src["kernel_pkg"], *src.get("firmware_pkgs", [])]
    boot_pkg = flash.lk2nd(entry)
    run.run([*base, "build", "--arch", "aarch64", *pkgs, *([boot_pkg] if boot_pkg else [])])
    # the initramfs is made by mkinitfs at install time, not shipped as a package
    run.run([*base, "install", "--no-image", "--password", "ztos"])
    if boot_pkg:
        # `build` only checks the binary repo; nothing installs lk2nd, so fetch it into the apk cache
        run.run([*base, "chroot", "-r", "--", "apk", "fetch", "-o", "/var/cache/apk", boot_pkg])
    if run.dry_run:
        return {"kernel": tree / "boot/vmlinuz", "initramfs": tree / "boot/initramfs", "dtb": tree / "dtb",
                "version": entry["kernel"].get("version", ""),
                "lk2nd": tree.parent / "lk2nd/boot/lk2nd.img" if boot_pkg else None}
    for pkg in pkgs:
        pmos.unpack_apk(_apk(work, pkg), tree)
    lk2nd_img = None
    if boot_pkg:
        # kept out of the kernel tree, which is copied into the root
        bl = tree.parent / "lk2nd"
        shutil.rmtree(bl, ignore_errors=True)
        bl.mkdir(parents=True)
        pmos.unpack_apk(_apk(work, boot_pkg), bl)
        lk2nd_img = bl / "boot/lk2nd.img"
        if not lk2nd_img.is_file():
            raise Exit(Exit.BUILD_FAILED, f"{boot_pkg} has no boot/lk2nd.img")
    chroot = work / f"chroot_rootfs_{pmb_device}"
    initramfs = chroot / "boot/initramfs"
    if not initramfs.is_file():
        raise Exit(Exit.BUILD_FAILED, f"no initramfs at {initramfs} after pmbootstrap install")
    (tree / "boot").mkdir(parents=True, exist_ok=True)
    shutil.copy(initramfs, tree / "boot/initramfs")
    return {**_find_kernel(tree, entry), "lk2nd": lk2nd_img}


def _apk(work: Path, pkg: str) -> Path:
    # built locally, or else fetched from the binary repo (pmbootstrap builds only what that lacks)
    apks = (sorted((work / "packages").glob(f"*/aarch64/{pkg}-[0-9]*.apk"))
            or sorted((work / "cache_apk_aarch64").glob(f"{pkg}-[0-9]*.apk"))
            or sorted(work.glob(f"chroot_rootfs_*/var/cache/apk/{pkg}-[0-9]*.apk")))
    if not apks:
        raise Exit(Exit.BUILD_FAILED, f"pmbootstrap left no {pkg}-*.apk in {work / 'packages'} "
                                      f"or {work / 'cache_apk_aarch64'}")
    return apks[-1]


def _find_kernel(tree: Path, entry: Entry, names: tuple = ("vmlinuz*",)) -> dict:
    kernels = sorted(k for n in names for k in (tree / "boot").glob(n))
    if not kernels:
        raise Exit(Exit.BUILD_FAILED, f"no boot/vmlinuz* in the kernel package (looked in {tree / 'boot'})")
    dtb = None
    name = entry["boot"].get("dtb", "").split()
    if name:
        for base in (tree / "boot/dtbs", tree / "usr/share/dtb", tree / "boot"):
            hit = list(base.glob(f"**/{name[0]}.dtb")) if base.is_dir() else []
            if hit:
                dtb = hit[0]
                break
        if dtb is None:
            raise Exit(Exit.BUILD_FAILED, f"dtb {name[0]}.dtb not found in the kernel package")
    modules = sorted((tree / "lib/modules").glob("*")) if (tree / "lib/modules").is_dir() else []
    return {"kernel": kernels[0], "initramfs": tree / "boot/initramfs", "dtb": dtb,
            "version": modules[0].name if modules else entry["kernel"].get("version", "")}


def verify(run: Runner, entry: Entry, art: dict, tree: Path, registry: Registry, step: str = "2. verify") -> str:
    """Step 2: one hash over kernel, modules and dtb."""
    run.step(step)
    if run.dry_run:
        return entry["kernel"].get("artifacts_sha256", "")
    parts = [art["kernel"], tree / "lib/modules"] + ([art["dtb"]] if art["dtb"] else [])
    digest = images.hash_tree(parts)
    want = entry["kernel"].get("artifacts_sha256", "")
    if want and want != digest:
        if entry.status == "tested":
            raise Exit(Exit.HASH_MISMATCH, f"kernel artifacts changed for a tested phone:\n  entry {want}\n"
                                           f"  built {digest}\nRefusing to build; run `ztconflo refresh` "
                                           f"if this is intended.")
        run.log(f"    hash changed on a {entry.status} entry: {want[:12]} -> {digest[:12]}")
    if want != digest and entry.status != "tested" and entry.path:
        entry["kernel"]["artifacts_sha256"] = digest
        entry["kernel"]["version"] = art["version"] or entry["kernel"].get("version", "")
        registry.write(dict(entry))
        run.log("    artifacts_sha256 written into the entry")
    return digest


def build(res: Resolution, settings: Settings, registry: Registry, run: Runner) -> Path:
    if settings.gsi:
        return build_gsi(res, settings, registry, run)
    start = time.time()
    entry = entry_for(res, settings, registry, run)
    for w in entry_warnings(entry):
        run.log(f"!!! {w}")
    codename = entry.codename
    out = settings.out / codename
    work = settings.work / codename
    tree, root = work / "kernel", work / "root"
    if not run.dry_run:
        out.mkdir(parents=True, exist_ok=True)
        if tree.exists():
            shutil.rmtree(tree)
        tree.mkdir(parents=True)
    art = fetch_kernel(run, entry, settings, tree)
    digest = verify(run, entry, art, tree, registry)

    run.step("3. root filesystem")
    run.run(["rm", "-rf", root], root=True)
    work.mkdir(parents=True, exist_ok=True) if not run.dry_run else None
    conf = rootfs.pacman_conf(work) if not run.dry_run else work / "pacman-aarch64.conf"
    rootfs.keyring(run, work)
    rootfs.pacstrap(run, root, conf, rootfs.os_packages(settings.ztos_src) + rootfs.packages_txt(entry.folder),
                    settings.jobs)
    rootfs.kernel_side(run, root, tree)
    rootfs.pacman_sandbox(run, root, tree)
    run.step("3b. ztMETRO, the SDK and the system apps, from source")
    rootfs.ztmetro_from_source(run, root, settings.ztos_src, settings.jobs)
    rootfs.system_apps(run, root)
    rootfs.overlay(run, root, settings.ztos_src, entry.folder / "overlay" if entry.folder else None)
    run.step("3c. Android apps (ztOSAndroidRuntimeV1: ATL)")
    rootfs.android_runtime(run, root, settings.ztos_src)

    run.step("4. configure")
    rootfs.configure(run, root, entry, Path.home() / ".ssh/id_ed25519.pub")

    run.step("5. initramfs (postmarketOS's, reused unchanged)")
    _images(run, entry, art, root, out, settings, start, digest)
    return out


def _images(run: Runner, entry: Entry, art: dict, root: Path, out: Path, settings: Settings, start: float,
            digest: str, extra: dict | None = None) -> None:
    """Steps 6 to 8, the same for both pipelines; `extra` joins build.json."""
    codename = entry.codename
    art = {**art, "initramfs": images.initramfs_with_updates(run, art["initramfs"], root, out.parent)}
    run.step("6. root image")
    on_lk2nd = bool(flash.lk2nd(entry))
    if on_lk2nd:
        # lk2nd boots extlinux from a boot partition inside userdata, as postmarketOS does
        root_img = images.rootfs_image(run, root, out.parent / f".{codename}-root.ext4")
        rootfs_img = images.extlinux_disk(run, entry["boot"], art, root_img, out.parent, out / "rootfs.img")
    else:
        rootfs_img = images.rootfs_image(run, root, out / "rootfs.img")
    if entry["boot"]["flash_method"] == "fastboot" or entry["boot"].get("sparse"):
        images.sparse(run, rootfs_img)

    run.step("7. boot image")
    if on_lk2nd:
        run.log("    none: lk2nd boots the kernel from the boot partition in rootfs.img")
        run.run(["rm", "-f", out / "boot.img"])
    else:
        images.boot_image(run, entry["boot"], art["kernel"], art["initramfs"], art["dtb"], out / "boot.img")
    if entry["boot"]["flash_method"] == "heimdall-isorec" and not run.dry_run:
        shutil.copy(art["kernel"], out / "kernel")
        shutil.copy(art["initramfs"], out / "initramfs")
    if art.get("lk2nd") and not run.dry_run:
        shutil.copy(art["lk2nd"], out / "lk2nd.img")

    run.step("8. output")
    if not run.dry_run:
        sh = out / "flash.sh"
        sh.write_text(flash.script(entry))
        sh.chmod(0o755)
        record = {
            "codename": codename, "status": entry.status, "origin": entry.origin,
            "built": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "seconds": round(time.time() - start),
            "source": entry.get("source", {}), "kernel": {**entry["kernel"], "artifacts_sha256": digest},
            "boot": entry["boot"], "ztos_src_commit": _git_head(settings.ztos_src),
            "images": {name: images.hash_tree([out / name]) for name in ("boot.img", "rootfs.img")
                       if (out / name).is_file()},
            **(extra or {}),
        }
        (out / "build.json").write_text(json.dumps(record, indent=2) + "\n")
    else:
        run.log(f"    (would write {out}/flash.sh and build.json)\n" + flash.script(entry))


# ---- the GSI pipeline (opt-in with --gsi) ---------------------------------------------------

def kernel_contract(run: Runner, entry: Entry, trees: tuple, g: "gsi.Gsi | None",
                    registry: Registry) -> contract.Result:
    """Step 3: the kernel's config against the GSI's contract. Hard gaps stop
    with exit 2; soft ones are reported, and binder goes into the entry."""
    run.step("3. kernel contract")
    result = contract.check(contract.find_config(*trees), entry["kernel"].get("gpu_driver"),
                            g.kernel_contract if g else None)
    for line in result.lines():
        run.log("    " + line)
    if not result.known:
        run.log("!!! no kernel config found: the kernel contract is unchecked (the GSI checks it again at boot)")
        return result
    if result.hard_failures:
        raise Exit(Exit.NOT_ELIGIBLE, "the kernel does not meet the GSI's contract: " + "; ".join(
            f"{n.title} needs {', '.join(result.missing[n.key])} ({n.without})" for n in result.hard_failures))
    for n in result.soft_gaps:
        run.log(f"!!! {n.title}: {n.without} (missing {', '.join(result.missing[n.key])})")
    binder = "binderfs" if result.has("waydroid") else "none"
    if entry.get("android", {}).get("binder") != binder and entry.path and not run.dry_run:
        entry.setdefault("android", {})["binder"] = binder
        entry["android"].setdefault("gralloc", "gbm")
        registry.write(dict(entry))
        run.log(f"    [android] binder = \"{binder}\" written into the entry")
    return result


def _native_kernel(root: Path, entry: Entry, dry_run: bool) -> dict:
    if dry_run:
        return {"kernel": root / "boot/Image", "initramfs": root / "boot/initramfs", "dtb": root / "dtb",
                "version": entry["kernel"].get("version", "")}
    art = _find_kernel(root, entry, ("vmlinuz*", "Image*"))
    initramfs = sorted((root / "boot").glob("initramfs*"))
    if not initramfs:
        raise Exit(Exit.BUILD_FAILED, f"the device layer left no boot/initramfs* in {root / 'boot'}")
    art["initramfs"] = initramfs[0]
    return art


def build_gsi(res: Resolution, settings: Settings, registry: Registry, run: Runner) -> Path:
    """GSI + device layer + the kernel's modules and firmware, checked by
    ztos-ial before the images are made."""
    start = time.time()
    entry = entry_for(res, settings, registry, run)
    for w in entry_warnings(entry):
        run.log(f"!!! {w}")
    codename = entry.codename
    out = settings.out / codename
    work = settings.work / codename
    tree, root = work / "kernel", work / "root"
    if not run.dry_run:
        out.mkdir(parents=True, exist_ok=True)
        if tree.exists():
            shutil.rmtree(tree)
        tree.mkdir(parents=True)

    run.step("1. GSI")
    g = gsi.fetch(settings.gsi, settings.gsi_cache, run.dry_run, run.log)
    run.log(f"    GSI {g.version}" + (f", sha256 {g.sha256[:16]}" if g.sha256 else "")
            + (" (verified)" if g.verified else ""))
    run.run(["rm", "-rf", root], root=True)
    run.run(["mkdir", "-p", root], root=True)
    run.run(["tar", "--numeric-owner", "-xpf", g.path, "-C", root], root=True)

    if entry.origin == "pmos":
        art = fetch_kernel(run, entry, settings, tree, step="2. kernel side")
        digest = verify(run, entry, art, tree, registry, step="2b. verify")
        trees = (tree,)
        layer = "generated from the registry entry"
    else:
        run.step("2. kernel side (the device layer)")
        layer = ial.install_native(run, root, entry, settings.ztos_repo, settings.device_layer, work)
        run.log(f"    device layer: {layer}")
        art = _native_kernel(root, entry, run.dry_run)
        digest = verify(run, entry, art, root, registry, step="2b. verify")
        trees = (root,)

    result = kernel_contract(run, entry, trees, g, registry)

    run.step("4. device layer")
    if entry.origin == "pmos":
        m, notes = ial.manifest(entry, result, g)
        ial.install_generated(run, root, entry, m, work, settings.pmaports)
    else:
        m, notes = (ial.read(root) if not run.dry_run else {}), []
        if not m and not run.dry_run:
            run.log(f"!!! the device layer has no /{ial.MANIFEST}")
    for n in notes:
        run.log(f"!!! {n}")

    run.step("5. assemble and check")
    if entry.origin == "pmos":
        rootfs.kernel_side(run, root, tree)
    overlay = entry.folder / "overlay" if entry.folder else None
    if overlay is not None and (run.dry_run or overlay.is_dir()):
        # owned by root, not by whoever checked the registry out
        run.run(["cp", "-a", "--no-preserve=ownership", f"{overlay}/.", root], root=True)
    # the Lumia photos are not in the GSI (not ours to ship): copied from a
    # ztOSMobile checkout that made them, as the S9's layer.sh does
    walls = settings.ztos_src / "build/wallpapers/backgrounds/ztos"
    if walls.is_dir():
        run.run(["install", "-d", root / "usr/local/share/backgrounds"], root=True)
        run.run(["cp", "-a", "--no-preserve=ownership", walls, root / "usr/local/share/backgrounds/"], root=True)
    else:
        run.log(f"    no wallpapers at {walls} (os/wallpapers/make-wallpapers.py makes them)")
    # the ringtones and UI sounds are Microsoft's too: in a checkout's
    # overlay (git-ignored), not in the GSI, which carries only the keyboard's
    sounds = settings.ztos_src / "os/overlay/usr/local/share/sounds/ztos"
    if (sounds / "phone").is_dir():
        run.run(["install", "-d", root / "usr/local/share/sounds"], root=True)
        run.run(["cp", "-a", "--no-preserve=ownership", sounds, root / "usr/local/share/sounds/"], root=True)
    else:
        run.log(f"    no ringtones at {sounds}/phone: the phone will have only keyboard sounds")
    extra_pkgs = rootfs.packages_txt(entry.folder)
    if extra_pkgs:
        # what the GSI does not carry and the phone needs (the J5: BlueZ);
        # no download sandbox, which wants Landlock from the kernel running pacman
        run.run(["arch-chroot", root, "pacman", "-S", "--needed", "--noconfirm", "--disable-sandbox",
                 *extra_pkgs], root=True)
    rootfs.pacman_sandbox(run, root, *trees)
    rootfs.configure(run, root, entry, Path.home() / ".ssh/id_ed25519.pub")
    ial_check(run, root)

    _images(run, entry, art, root, out, settings, start, digest, {
        "gsi": g.record(), "contract": result.to_dict(), "manifest": m, "device_layer": layer,
    })
    return out


def ial_check(run: Runner, root: Path) -> None:
    """ztos-ial check inside the assembled root, under qemu; a failure is exit 3."""
    if not run.dry_run and not (root / "usr/bin/ztos-ial").exists():
        run.log("    the GSI has no /usr/bin/ztos-ial: ztOSIAL check skipped")
        return
    r = run.run(["arch-chroot", root, "/usr/bin/ztos-ial", "check", "--root", "/"], root=True, check=False)
    if r.returncode != 0:
        raise Exit(Exit.BUILD_FAILED, f"ztos-ial check failed ({r.returncode}): the device layer does not give "
                                      f"the GSI what it needs; its output is above")


def _git_head(path: Path) -> str:
    import subprocess
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""
