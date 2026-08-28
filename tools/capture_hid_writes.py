"""Temporarily observe 64-byte lighting packets written by the official driver."""

from __future__ import annotations

import argparse
import time

import frida


SCRIPT = r"""
const writeFile = Module.getGlobalExportByName('WriteFile');

Interceptor.attach(writeFile, {
  onEnter(args) {
    const length = args[2].toUInt32();
    if (length !== 64) return;
    const data = args[1].readByteArray(length);
    if (data === null) return;
    const bytes = new Uint8Array(data);
    if (bytes[0] !== 0x01 || bytes[1] !== 0x07) return;
    send({
      kind: 'lighting',
      hex: Array.from(bytes, value => value.toString(16).padStart(2, '0')).join(' ')
    });
  }
});
send({kind: 'ready'});
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--seconds", type=float, default=120.0)
    args = parser.parse_args()

    session = frida.attach(args.pid)
    script = session.create_script(SCRIPT)

    def on_message(message: dict, data: bytes | None) -> None:
        if message.get("type") == "send":
            payload = message.get("payload", {})
            if payload.get("kind") == "ready":
                print("READY", flush=True)
            elif payload.get("kind") == "lighting":
                print(f"PACKET {time.time():.3f} {payload['hex']}", flush=True)
        else:
            print(message, flush=True)

    script.on("message", on_message)
    script.load()
    try:
        time.sleep(max(0.1, args.seconds))
    except KeyboardInterrupt:
        pass
    finally:
        session.detach()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
