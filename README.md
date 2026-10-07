# Przewijak — ARTYKUŁY V15.1 PC APP

Rozwinięcie V15.0 MULTISOURCE bez przebudowy silnika pobierania i bez zmiany schematu bazy artykułów.

## Co zmieniono w tym etapie

- **PC ma własne okno aplikacji** (`desktop_app.py`) zamiast otwierania panelu w zewnętrznej przeglądarce.
- Okno korzysta z Windows WebView2 przez `pywebview`; lokalny panel HTTP jest tylko wewnętrzną warstwą UI.
- **CORE V15.0 pozostał wspólny**: pobieranie, selektory, źródła, deduplikacja, SQLite, archiwum i wybór folderu nie zostały przepisane.
- Zachowane jest automatyczne wyszukiwanie wolnego portu.
- Dodany jest skrypt `BUDUJ_EXE_WINDOWS.bat` tworzący `Przewijak_ARTYKULY_PC.exe`.
- Dodany jest workflow GitHub Actions `Zbuduj aplikacje PC EXE`, który buduje gotową paczkę Windows na `windows-latest`.
- Dodana jest własna ikona aplikacji w `assets/przewijak.ico`.

## Uruchomienie na Windows z kodu

1. Uruchom `INSTALUJ_WINDOWS.bat`.
2. Uruchom `START_WINDOWS.bat`.
3. Program otworzy się we własnym oknie **Przewijak — ARTYKUŁY**.

Nie trzeba ręcznie wpisywać adresu localhost ani otwierać panelu w Edge/Chrome.

## Budowanie prawdziwego EXE

Uruchom `BUDUJ_EXE_WINDOWS.bat` na Windows. Wynik:

`dist\Przewijak_ARTYKULY_PC\Przewijak_ARTYKULY_PC.exe`

Wybrano wariant `onedir`, a nie pojedynczy `onefile`, bo jest stabilniejszy dla WebView2 i łatwiejszy do diagnozowania. Użytkownik nadal uruchamia jeden plik EXE znajdujący się w gotowym folderze aplikacji.

### GitHub

Workflow `.github/workflows/build-windows.yml` może zbudować tę samą paczkę bez lokalnej instalacji PyInstallera. W Actions uruchom workflow **Zbuduj aplikacje PC EXE** i pobierz artefakt `Przewijak_ARTYKULY_PC`.

## Dane użytkownika

Baza historii pozostaje w dotychczasowej lokalizacji Windows:

`%LOCALAPPDATA%\PrzewijakArtykuly\baza_artykulow.sqlite3`

Aktualizacja programu nie powinna kasować tej bazy. Folder archiwum artykułów nadal wybiera użytkownik.

## PHONE / TRANSFER

Android nadal jest etapem następnym. Ta wersja **nie udaje APK** i nie przenosi telefonu na Termux jako rozwiązania docelowego. Dokumentacja wspólnego formatu pozostaje w `docs/PLAN_PC_PHONE_TRANSFER.md` oraz `docs/TRANSFER_V1.md`.

Docelowa kolejność:

1. ustabilizować i przetestować V15.1 PC APP,
2. wdrożyć TRANSFER do wspólnego modułu,
3. zbudować natywne APK Android bez Termuxa,
4. test zgodności danych PC ↔ PHONE.
