import os
import threading

os.environ["PRZEWIJAK_ANDROID_NATIVE"] = "1"

import app as core

_lock = threading.RLock()
_server = None
_thread = None
_port = 0


def start_server():
    global _server, _thread, _port
    with _lock:
        if _server is not None and _port:
            return _port
        _server, _port = core.create_server_on_free_port(core.HOST, core.PORT)
        _thread = threading.Thread(
            target=_server.serve_forever,
            name="PrzewijakAndroidHTTP",
            daemon=True,
        )
        _thread.start()
        return _port


def get_port():
    return int(_port or start_server())


def get_output_root():
    return str(core.DL.output_root)


def set_saf_target(uri, label=""):
    # Android/Java odpowiada za kopiowanie do SAF. CORE nadal zapisuje atomowo
    # w prywatnym katalogu aplikacji, dzięki czemu przerwanie kopiowania nie
    # uszkodzi lokalnej bazy ani artykułu.
    core.DL.storage_mode = "android-saf"
    core.DL.saf_root_uri = str(uri or "")
    core.DL.saf_label = str(label or "wybrany folder")
    core.DL._set_status(
        "Folder Android/SAF ustawiony. Artykuły są zapisywane lokalnie i synchronizowane do wybranego katalogu."
    )
    return core.DL.output_display()


def restore_saf_target(uri, label=""):
    if uri:
        return set_saf_target(uri, label)
    return core.DL.output_display()


def clear_saf_target():
    core.DL.storage_mode = "filesystem"
    core.DL.saf_root_uri = ""
    core.DL.saf_label = ""
    return core.DL.output_display()
