#!/usr/bin/env python3
"""Run with Python 3.11+ and FFmpeg on PATH (or bundled beside Offset.exe)."""
import argparse
import json
import os
import sys
import threading
import webbrowser

from offset.relay import Config, Relay
from offset.server import make_server
from offset.settings import SettingsStore
from offset.tray import SystemTray


def main():
    frozen = getattr(sys, "frozen", False)
    parser = argparse.ArgumentParser(description="Offset: relay RTMP local con delay dinámico en vivo.")
    parser.add_argument("--delay", type=float, default=0, help="Retraso inicial en segundos (0 = en directo).")
    parser.add_argument("--default-delay", type=float, default=30, help="Retraso aplicado al pulsar Activar Delay.")
    parser.add_argument("--memory-mb", type=int, default=128)
    parser.add_argument("--input", default="rtmp://127.0.0.1:1935/live/input")
    parser.add_argument("--output", default="rtmp://127.0.0.1:1936/live/offset")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", default=frozen, help="Abre el panel automáticamente.")
    parser.add_argument("--no-open", action="store_true", help="No abrir navegador al iniciar.")
    parser.add_argument("--tray", action="store_true", default=(frozen and os.name == "nt"),
                        help="Ejecuta con icono en la bandeja del sistema de Windows.")
    parser.add_argument("--console", action="store_true", help="Desactiva el modo bandeja y usa consola.")
    parser.add_argument("--strict-bframes", action="store_true", help="Rechaza streams con B-frames.")
    parser.add_argument("--local-only", action="store_true", help="Restringe la salida solo a 127.0.0.1.")
    parser.add_argument("--no-reconnect", action="store_true", help="Desactiva la reconexión automática.")
    parser.add_argument("--headless", action="store_true", help="Inicia sin panel HTTP.")
    parser.add_argument("--input-file", help="Fixture FLV de prueba, leído en tiempo real.")
    parser.add_argument("--output-file", help="Archivo FLV nuevo de prueba; no se sobrescribe.")
    parser.add_argument("--audit-file", help="JSONL de prueba; máximo 500000 paquetes.")
    args = parser.parse_args()
    try:
        config = Config(
            delay=args.delay,
            memory_mb=args.memory_mb,
            input_url=args.input,
            output_url=args.output,
            ffmpeg=args.ffmpeg,
            allow_bframes=not args.strict_bframes,
            allow_remote_output=not args.local_only,
            default_delay=args.default_delay,
            auto_reconnect=not args.no_reconnect and not args.headless,
        )
        config.validate()
        if args.headless:
            relay = Relay(config, input_file=args.input_file,
                          output_file=args.output_file, audit_file=args.audit_file)
            relay.start()
            try:
                while not relay.done.wait(.5):
                    pass
            except KeyboardInterrupt:
                relay.stop()
            print(json.dumps(relay.snapshot(), ensure_ascii=False))
            code = 1 if relay.state == "FAULT" else 0
            relay.stop()
            return code
        if args.input_file or args.output_file or args.audit_file:
            parser.error("Las opciones de archivo requieren --headless.")

        server = make_server(config, args.port, settings_store=SettingsStore())
        url = f"http://127.0.0.1:{server.server_port}"
        use_tray = args.tray and not args.console and os.name == "nt"

        if not frozen:
            print(f"Offset local: {url}", flush=True)
            print(f"Dock para OBS: {url}/?dock=1", flush=True)
            print("Configura tu destino o inicia directo con OBS.", flush=True)

        if args.open and not args.no_open:
            webbrowser.open(url)

        if use_tray:
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .25}, daemon=True)
            worker.start()

            def shutdown_all():
                server.controller.shutdown()
                server.shutdown()
                server.server_close()

            tray = SystemTray(server, on_exit=shutdown_all)
            try:
                tray.run_blocking()
            except KeyboardInterrupt:
                tray.stop()
            return 0

        try:
            server.serve_forever(poll_interval=.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.controller.shutdown()
            server.server_close()
        return 0
    except (ValueError, OSError) as exc:
        if not frozen:
            print(f"No se pudo iniciar Offset: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
