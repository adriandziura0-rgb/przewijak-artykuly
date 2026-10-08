# Przewijak — ARTYKUŁY V15.3 PHONE

To jest natywna aplikacja Android, bez Termuxa.

## Architektura

- wspólny CORE: główny `app.py` z repozytorium,
- Python osadzony w APK przez Chaquopy,
- ekran: Android WebView z lokalnym panelem 127.0.0.1,
- praca w tle: foreground service z widocznym powiadomieniem,
- folder/SD: systemowy Android Storage Access Framework (ACTION_OPEN_DOCUMENT_TREE),
- zapis bezpieczny: najpierw prywatny katalog aplikacji, potem synchronizacja do wybranego folderu SAF,
- synchronizacja SAF co 30 sekund oraz natychmiast po wyborze folderu,
- deduplikacja i 25 źródeł: ten sam kod V15.3 co PC.

## Ważne

Android może ograniczyć aplikację przy agresywnym oszczędzaniu baterii producenta. Foreground service znacząco poprawia trwałość pracy w tle, ale system nadal zachowuje ostateczną kontrolę nad procesami.

## Build

GitHub Actions: workflow **Build APK ARTYKULY PHONE**.

Wynik: `Przewijak_ARTYKULY_PHONE_V15_3.apk`.
