#!/usr/bin/env python3
"""Complete local RTMP live demo showing dynamic delay ON/OFF without OBS.

Generates 30 s of real H.264 (with B-frames!) + AAC, starts LIVE (0 s),
activates a 4 s delay via slate at t=5 s (0 frames lost), then runs lossless
catch-up back to 0 s at t=14 s, saving the continuous output FLV.
"""
import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import threading
import time
import webbrowser

from offset.relay import Config
from offset.server import make_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", action="store_true", help="Abre el panel en el navegador.")
    parser.add_argument("--static", action="store_true", help="Ejecuta la demo clásica de retraso fijo 5 s.")
    args = parser.parse_args()
    Config().validate()
    output = Path.cwd() / ("offset-demo-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".flv")
    if output.exists():
        raise ValueError("El archivo de demostración ya existe.")
    init_delay = 5 if args.static else 0
    server = make_server(Config(delay=init_delay, default_delay=4, catchup_rate=1.35))
    publisher = receiver = None
    try:
        receiver = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-listen", "1", "-i",
             "rtmp://127.0.0.1:1936/live/offset", "-map", "0:v", "-map", "0:a",
             "-c", "copy", "-f", "flv", str(output)],
            stdout=subprocess.DEVNULL,
        )
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        server.controller.start({"delay": init_delay})
        time.sleep(.5)
        print("Panel: http://127.0.0.1:8765  |  OBS Dock: http://127.0.0.1:8765/?dock=1", flush=True)
        if args.static:
            print("Demo estática: 20 s de señal, 5 s de retraso fijo. Ctrl+C para salir.", flush=True)
            duration = 20
        else:
            print("Demo dinámica: 30 s en vivo → activa delay 4 s con slate → alcanza 0 s sin perder frames.", flush=True)
            duration = 30
        if args.open:
            webbrowser.open("http://127.0.0.1:8765")
        publisher = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i",
             "avsynctest=size=640x360:framerate=30:samplerate=48000:period=1[out0][out1]",
             "-map", "0:v", "-map", "0:a", "-t", str(duration), "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", "-bf", "2", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
             "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-f", "flv",
             "rtmp://127.0.0.1:1935/live/input"],
            stdout=subprocess.DEVNULL,
        )

        if not args.static:
            def script_transitions():
                time.sleep(5)
                if server.controller.relay and not server.controller.relay.done.is_set():
                    print(">> Activando delay de 4 s con slate...", flush=True)
                    server.controller.enable_delay({"delay": 4})
                time.sleep(10)
                if server.controller.relay and not server.controller.relay.done.is_set():
                    print(">> Desactivando delay (modo sin perder frames a 1.35x)...", flush=True)
                    server.controller.disable_delay({"mode": "catchup", "catchup_rate": 1.35})
            threading.Thread(target=script_transitions, daemon=True).start()

        while not server.controller.relay.done.wait(.5):
            if publisher.poll() not in (None, 0):
                server.controller.relay.fail("Falló el generador de prueba. Comprueba libx264 y avsynctest.")
                break
        status = server.controller.status()
        if status["state"] == "FINISHED":
            receiver.wait(timeout=10)
            print(f"Grabación continua lista: {output}", flush=True)
            print("Ábrela con VLC o ffplay. El panel permanece abierto hasta Ctrl+C.", flush=True)
        else:
            print("Prueba interrumpida: " + str(status.get("error") or status["state"]), flush=True)
        while True:
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.controller.shutdown()
        for proc in (publisher, receiver):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
