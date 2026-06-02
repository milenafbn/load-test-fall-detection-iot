"""Container entrypoint that applies optional Linux traffic control settings."""

import os
import shlex
import shutil
import subprocess
import sys


DEFAULT_SENTINEL = "run-load-test"


def _env_number(name: str, default: float = 0.0, suffixes: tuple[str, ...] = ()) -> float:
    raw = os.getenv(name, str(default)).strip()
    if not raw:
        return default

    lowered = raw.lower()
    for suffix in suffixes:
        if lowered.endswith(suffix):
            raw = raw[: -len(suffix)]
            break

    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"[tc] Invalid value for {name}: {os.getenv(name)!r}")

    if value < 0:
        raise SystemExit(f"[tc] {name} must be >= 0")
    return value


def _strict_mode() -> bool:
    raw = os.getenv("TC_STRICT", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _tc_enabled() -> bool:
    raw = os.getenv("TC_ENABLED", "auto").strip().lower()
    if raw in {"0", "false", "no", "off", "disabled"}:
        return False
    return True


def _default_command() -> list[str]:
    return [
        sys.executable,
        "scripts/load_test.py",
        "--devices",
        os.getenv("DEVICE_COUNT", "333"),
        "--offset",
        os.getenv("DEVICE_OFFSET", "0"),
        "--requests",
        os.getenv("REQUESTS", "1000"),
        "--interval",
        os.getenv("INTERVAL_MS", "100"),
    ]


def _build_netem_args() -> list[str]:
    latency_ms = _env_number("TC_LATENCY_MS", suffixes=("ms",))
    jitter_ms = _env_number("TC_JITTER_MS", suffixes=("ms",))
    loss_pct = _env_number("TC_LOSS_PCT", suffixes=("%",))
    rate = os.getenv("TC_RATE", "").strip()

    if loss_pct > 100:
        raise SystemExit("[tc] TC_LOSS_PCT must be <= 100")

    args: list[str] = []

    if latency_ms > 0 or jitter_ms > 0:
        args.extend(["delay", f"{latency_ms:g}ms"])
        if jitter_ms > 0:
            args.extend([f"{jitter_ms:g}ms", "distribution", "normal"])

    if loss_pct > 0:
        args.extend(["loss", f"{loss_pct:g}%"])

    if rate:
        args.extend(["rate", rate])

    return args


def _apply_traffic_control() -> None:
    if not _tc_enabled():
        print("[tc] disabled by TC_ENABLED", flush=True)
        return

    netem_args = _build_netem_args()
    if not netem_args:
        print("[tc] disabled: no latency, jitter, loss or rate configured", flush=True)
        return

    tc_bin = shutil.which("tc")
    strict = _strict_mode()
    if not tc_bin:
        message = "[tc] tc binary not found. Rebuild the image with iproute2 installed."
        if strict:
            raise SystemExit(message)
        print(f"{message} Continuing because TC_STRICT=false.", flush=True)
        return

    iface = os.getenv("TC_IFACE", "eth0").strip() or "eth0"
    subprocess.run(
        [tc_bin, "qdisc", "del", "dev", iface, "root"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    command = [tc_bin, "qdisc", "replace", "dev", iface, "root", "netem", *netem_args]
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        message = (
            "[tc] failed to apply traffic control. "
            "Make sure the container has cap_add: NET_ADMIN. "
            f"Command: {shlex.join(command)}"
        )
        if strict:
            raise SystemExit(message) from exc
        print(f"{message} Continuing because TC_STRICT=false.", flush=True)
        return

    show = subprocess.run(
        [tc_bin, "qdisc", "show", "dev", iface],
        capture_output=True,
        text=True,
        check=False,
    )
    print(f"[tc] applied on {iface}: {' '.join(netem_args)}", flush=True)
    if show.stdout.strip():
        print(f"[tc] {show.stdout.strip()}", flush=True)


def main() -> None:
    _apply_traffic_control()

    args = sys.argv[1:]
    command = _default_command() if not args or args == [DEFAULT_SENTINEL] else args
    print(f"[entrypoint] running: {shlex.join(command)}", flush=True)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
