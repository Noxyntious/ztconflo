# SPDX-License-Identifier: GPL-2.0-or-later
"""The hardware checklist, run on a connected phone, and the pull request that
records it in the registry. Never pushes to the main branch."""
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from . import Exit
from .config import USB_HOST
from .registry import Entry, Registry, advance

LOG = "$HOME/.local/state/ztmetro.log"
MANIFEST = "/vendor/ztos/manifest.toml"     # only GSI builds have one
ANDROID_TEST_APP = "com.android.settings"


@dataclass
class Check:
    key: str
    title: str
    needed_for: str                 # "boots" or "tested"
    command: str = ""               # automatic part: exit 0 and output pass `ok`
    ok: Callable[[str], bool] = lambda out: True
    question: str = ""              # manual part, asked of the person holding the phone
    optional: bool = False
    prepare: str = ""               # run on the phone before asking (a tone, a buzz)
    applies: str = ""               # exit 0 when the check applies to this image; else recorded as n/a


CHECKS = [
    Check("ztmetro", "Boots to ztMETRO", "boots",
          f"pgrep -x ztmetro >/dev/null && grep -c 'showing Start' {LOG}", lambda o: o.strip() not in ("", "0")),
    Check("display", "Display", "boots",
          "for c in /sys/class/drm/card*-*; do [ \"$(cat $c/status 2>/dev/null)\" = connected ] && "
          "head -1 $c/modes; done", lambda o: "x" in o),
    Check("touch", "Touch", "boots",
          question="Tap the three corner targets on the screen (top left, top right, bottom middle). "
                   "Did every tap register?"),
    Check("gpu", "GPU renderer", "tested",
          f"grep -m1 'renderer: ' {LOG}", lambda o: bool(o.strip()) and "llvmpipe" not in o.lower()),
    Check("wifi", "Wi-Fi", "tested",
          "nmcli -t -f SSID dev wifi list --rescan yes 2>/dev/null | grep -c .", lambda o: o.strip() not in ("", "0")),
    Check("vibrator", "Vibrator", "tested",
          "grep -l . /sys/class/input/event*/device/capabilities/ff 2>/dev/null | "
          "xargs -r grep -L '^0$' | head -1", lambda o: bool(o.strip()),
          question="Did the phone buzz just now?",
          prepare="dev=$(for e in /sys/class/input/event*; do c=$(cat $e/device/capabilities/ff 2>/dev/null); "
                  "[ -n \"$c\" ] && [ \"$c\" != 0 ] && echo /dev/input/${e##*/} && break; done); "
                  "[ -n \"$dev\" ] && command -v fftest >/dev/null && (echo 0 | timeout -k 2 2 fftest $dev >/dev/null 2>&1) "
                  "|| true"),
    # -k: speaker-test can sit in its drain after the tone and not take the
    # TERM timeout sends; without a KILL after it the check waits forever
    Check("audio", "Audio", "tested",
          question="A test tone is playing. Did you hear it?",
          prepare="timeout -k 2 3 speaker-test -t sine -f 660 -l 1 >/dev/null 2>&1 || "
                  "timeout -k 2 3 pw-play /usr/share/sounds/freedesktop/stereo/bell.oga >/dev/null 2>&1 || true"),
    Check("battery", "Battery reporting", "tested",
          "cat /sys/class/power_supply/*/capacity 2>/dev/null | head -1", lambda o: o.strip().isdigit()),
    Check("suspend", "Suspend and resume", "tested",
          question="Press power to sleep the phone, wake it again, and tap something. "
                   "Did the screen come back with touch working?"),
    Check("modem", "Modem", "tested",
          "mmcli -L 2>/dev/null | grep -c '/Modem/'", lambda o: o.strip() not in ("", "0"), optional=True),
    # every interface the manifest declares is called; a pre-GSI image has no manifest, so nothing to call
    Check("ial", "ztOSIAL interfaces", "tested",
          "ztos-ial test 2>&1", lambda o: "FAIL" not in o,
          question="ztos-ial test stepped the brightness and pulsed the vibrator, where the manifest declares "
                   "them. Did you see and feel both?",
          applies=f"[ -f {MANIFEST} ] && command -v ztos-ial >/dev/null"),
    Check("android", "Android apps", "tested",
          "timeout 180 sh -c 'until waydroid status 2>/dev/null | grep -q \"Session:.*RUNNING\"; do sleep 3; done' "
          f"&& waydroid app launch {ANDROID_TEST_APP} && sleep 8 && sudo -n waydroid shell pidof {ANDROID_TEST_APP}",
          lambda o: any(w.isdigit() for w in o.split()), optional=True,
          applies=f"grep -q '^binder = \"binderfs\"' {MANIFEST} 2>/dev/null && command -v waydroid >/dev/null"),
]


class Phone:
    """Commands on the phone over SSH (USB networking by default)."""

    def __init__(self, host: str = USB_HOST, user: str = "alarm", run=subprocess.run):
        self.target = f"{user}@{host}"
        self._run = run

    def sh(self, command: str) -> tuple[int, str]:
        r = self._run(["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
                       self.target, command], capture_output=True, text=True, timeout=120)
        return r.returncode, (r.stdout or "") + (r.stderr or "")


def run_checks(phone: Phone, ask=input, log=print) -> dict:
    results = {}
    for c in CHECKS:
        verdict = "pass"
        if c.applies and phone.sh(c.applies)[0] != 0:
            results[c.key] = "n/a"      # not on this image: no manifest, or no binder
            log(f"  {c.title:<22} n/a")
            continue
        if c.command:
            code, out = phone.sh(c.command)
            if code != 0 or not c.ok(out):
                verdict = "fail"
            log(f"  {c.title:<22} automatic: {verdict}" + ("" if verdict == "pass" else f"  ({out.strip()[:80]})"))
        if c.question and verdict == "pass":
            if c.prepare:
                phone.sh(c.prepare)
            answer = ask(f"  {c.title}: {c.question} [y/n] ").strip().lower()
            verdict = "pass" if answer.startswith("y") else "fail"
        if verdict == "fail" and c.optional:
            verdict = "absent"          # recorded, not blocking
        results[c.key] = verdict
    return results


def new_status(results: dict) -> str:
    boots = all(results.get(c.key) == "pass" for c in CHECKS if c.needed_for == "boots")
    if not boots:
        return "untested"
    tested = all(results.get(c.key) in ("pass", "n/a") for c in CHECKS if not c.optional)
    return "tested" if tested else "boots"


def promote(entry: Entry, registry: Registry, phone: Phone, ask=input, log=print, pr=True) -> str:
    log(f"Checking {entry['device']['name']} ({entry.codename}) on {phone.target}:")
    if phone.sh("true")[0] != 0:
        raise Exit(Exit.CHECKLIST_FAILED, f"cannot reach the phone over SSH at {phone.target}")
    results = run_checks(phone, ask, log)
    reached = new_status(results)
    status = advance(entry.status, reached)
    data = dict(entry)
    data["device"]["status"] = status
    data["verified"] = {"date": time.strftime("%Y-%m-%d"), "by": _git_user(registry), "checklist": results}
    registry.write(data)
    log(f"Result: {reached} (entry status: {status})")
    if pr:
        open_pull_request(registry, entry.codename, status, results, log)
    if reached == "untested":
        raise Exit(Exit.CHECKLIST_FAILED, "the boot checks did not all pass; status unchanged")
    return status


def _git_user(registry: Registry) -> str:
    r = registry.git("config", "user.name", check=False)
    return r.stdout.strip() or "unknown"


def open_pull_request(registry: Registry, codename: str, status: str, results: dict, log=print) -> None:
    """A branch, a commit, a push of that branch and a PR. Never the main branch."""
    branch = f"promote/{codename}-{time.strftime('%Y%m%d-%H%M%S')}"
    registry.git("checkout", "-b", branch)
    registry.git("add", f"devices/{codename}")
    registry.git("commit", "-m", f"{codename}: {status}")
    if registry.git("remote", check=False).stdout.strip() == "":
        log(f"The registry has no git remote: the result is committed on branch {branch}, not pushed.")
        return
    registry.git("push", "-u", "origin", branch)
    body = "\n".join(f"- {k}: {v}" for k, v in results.items())
    r = subprocess.run(["gh", "pr", "create", "--title", f"{codename}: {status}", "--body", body,
                        "--head", branch], cwd=registry.root, capture_output=True, text=True)
    log(r.stdout.strip() or f"gh pr create failed: {r.stderr.strip()}")
