#!/usr/bin/env python3
"""Przewijak — ARTYKUŁY / natywne okno Windows.

Warstwa desktopowa. Nie zmienia silnika pobierania ani schematu bazy V15.0.
Uruchamia lokalny serwer CORE na wolnym porcie i osadza panel w oknie WebView2.
"""
import os
import threading
import time

# Nie pozwalamy app.py samodzielnie otwierać zewnętrznej przeglądarki.
os.environ["PRZEWIJAK_EMBEDDED"] = "1"

import app as core


def main():
    try:
        import webview
    except Exception as exc:
        raise SystemExit(
            "Brak składnika pywebview. Uruchom INSTALUJ_WINDOWS.bat albo zbuduj EXE. "
            f"Szczegół: {type(exc).__name__}: {exc}"
        )

    server, actual_port = core.create_server_on_free_port(core.HOST, core.PORT)
    panel_url = f"http://{core.HOST}:{actual_port}"
    server_thread = threading.Thread(target=server.serve_forever, name="PrzewijakCoreHTTP", daemon=True)
    server_thread.start()

    def shutdown():
        try:
            core.DL.stop()
        except Exception:
            pass
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass

    try:
        window = webview.create_window(
            "Przewijak — ARTYKUŁY",
            panel_url,
            width=1080,
            height=860,
            min_size=(760, 620),
            resizable=True,
            confirm_close=False,
            text_select=True,
        )
        window.events.closed += shutdown
        # Na Windows pywebview użyje WebView2, jeżeli runtime jest dostępny.
        webview.start(debug=False, private_mode=False)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
