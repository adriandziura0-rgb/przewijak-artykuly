# Przewijak — ARTYKUŁY

Nowy, osobny projekt pobierania i archiwizacji artykułów. Baza wyjściowa: przesłany `Przewijak_Artykuly_V15_0_ETAP1_MULTISOURCE_CLEAN.zip`, V15.0 MULTISOURCE.

## Stan projektu

- Kod V15.0 i sposób wyboru folderu zachowane bez zmian. Wiele zapisanych źródeł, trwałe ustawienia i relacje article_sources; źródła uruchamiane ręcznie.
- Obecny panel PC otwiera się w przeglądarce; ta paczka nie zawiera EXE ani APK.
- Android: skrypty dla Termuxa i integracja SAF; brak APK.
- Wymiana TRANSFER PC/PHONE jest zaplanowana, jeszcze nie wdrożona.
- Nie jest to projekt LIVE eSoccer. Nie używa identyfikatorów meczów.

## Uruchomienie PC

Rozpakuj projekt, uruchom `INSTALUJ_WINDOWS.bat`, a następnie `START_WINDOWS.bat`. Pierwszy start wymaga Pythona oraz internetu do pobrania zależności. Szczegóły: `README_ETAP1_MULTISOURCE.txt`.

## Rozwój

Plan: [docs/PLAN_PC_PHONE_TRANSFER.md](docs/PLAN_PC_PHONE_TRANSFER.md).
Format wymiany: [docs/TRANSFER_V1.md](docs/TRANSFER_V1.md).
Pochodzenie kodu: [docs/POCHODZENIE.json](docs/POCHODZENIE.json).

GitHub Actions sprawdza składnię Pythona. Ten test nie potwierdza skuteczności pobierania z serwisów ani działania na urządzeniu Android.
