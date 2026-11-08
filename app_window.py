"""Linda as a native macOS window with local ears.   python app_window.py
Starts the server in a background thread with server.local_ears forced ON (Whisper + mic in Python,
voice from Python), then shows ui/ in a native window (pywebview). Chrome is not involved at all.
    pip install pywebview
"""
import socket, sys, threading, time

from dotenv import load_dotenv

from linda.config import ROOT, load
from server import build_deps, create_app


def wait_port(host, port, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    load_dotenv(ROOT / ".env")
    cfg = load()
    cfg["server"]["local_ears"] = True        # always local in this window
    cfg["server"]["open_browser"] = False     # we open our own window
    host, port = cfg["server"]["host"], cfg["server"]["port"]
    url = f"http://{host}:{port}"

    import uvicorn
    server = uvicorn.Server(uvicorn.Config(create_app(cfg, build_deps(cfg)), host=host, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()   # uvicorn skips signal handlers off the main thread
    if not wait_port(host, port):
        sys.exit("Server did not start. Look at the messages above.")

    try:
        import webview
    except ImportError:
        sys.exit("Run: pip install pywebview")
    webview.create_window("Linda", url, width=1180, height=780, min_size=(900, 600), background_color="#0b0d14")
    webview.start()                           # blocks until the window is closed (must run on the main thread)
    server.should_exit = True
    time.sleep(0.5)


if __name__ == "__main__":
    main()