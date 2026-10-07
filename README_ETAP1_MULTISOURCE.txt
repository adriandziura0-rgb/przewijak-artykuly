PRZEWIJAK — ARTYKUŁY V15.0
ETAP 1 — MULTISOURCE / CLEAN

CO ZOSTAŁO WDROŻONE
1. WIELE ŹRÓDEŁ
- W panelu można zapisać wiele serwisów/list artykułów.
- Każde źródło ma własną nazwę, URL, selektor, tryb listy, limity i odstęp GET.
- Źródła można WCZYTAĆ, WŁĄCZYĆ/WYŁĄCZYĆ i USUNĄĆ.
- Usunięcie źródła z listy NIE usuwa pobranych artykułów ani historii deduplikacji.

2. TRWAŁA KONFIGURACJA
- Formularz jest zapisywany w SQLite (app_settings).
- Odświeżenie panelu i restart programu nie kasują ustawień.
- Zapisane źródła są przechowywane w SQLite (sources).

3. DEDUPLIKACJA WIELOŹRÓDŁOWA
- Dotychczasowa globalna baza artykułów pozostaje zachowana.
- Ten sam canonical URL / hash treści nie jest pobierany ponownie.
- Dodano article_sources: jeden artykuł może być odnotowany przy wielu źródłach bez duplikowania treści.
- Starsze rekordy bazy są migrowane automatycznie; artykuły nie są kasowane.

4. WOLNY PORT
- Start zaczyna od 8765.
- Jeśli port jest zajęty, program próbuje 8766, 8767 itd.
- Panel otwierany jest pod rzeczywiście wybranym portem.

5. STAN ŹRÓDŁA
- Zapisywany jest ostatni status i czas uruchomienia źródła.
- Panel pokazuje liczbę artykułów przypisanych do danego źródła.

CO CELOWO NIE ZOSTAŁO JESZCZE DODANE
- Automatyczne cykliczne obchodzenie wszystkich włączonych źródeł.
- Scheduler per źródło i harmonogram minutowy.
- Uniwersalny SCROLL / LOAD MORE dla dowolnej strony.
To jest zakres ETAPU 2 i 3. W Etapie 1 wybrane źródło uruchamia się ręcznie.

CO NIE ZOSTAŁO PRZEBUDOWANE
- Ekstraktor artykułu.
- LOW-IMPACT / READ-ONLY.
- STOP na 403/429.
- TVN24 / TVP profile.
- Zapis raw.html / artykul.txt / artykul.html / metadata.json.
- Windows picker oraz Android SAF / SD.

OBSŁUGA
1. Uruchom jak poprzednio START_WINDOWS.bat albo START_ANDROID.sh.
2. Wpisz nazwę i URL pierwszego serwisu.
3. Ustaw parametry.
4. Kliknij „ZAPISZ / AKTUALIZUJ ŹRÓDŁO”.
5. Dodaj następne przez „NOWE ŹRÓDŁO”.
6. Przy konkretnym źródle kliknij „WCZYTAJ”.
7. Użyj „TEST WYBRANEGO” albo „START WYBRANEGO”.

TESTY V15.0
- py_compile app.py: PASS
- składnia JavaScript panelu (node --check): PASS
- zapis 2+ źródeł do SQLite: PASS
- aktualizacja źródła bez zmiany ID: PASS
- włączanie/wyłączanie źródeł: PASS
- trwałość konfiguracji po ponownym utworzeniu Downloadera: PASS
- relacja jeden artykuł -> wiele źródeł: PASS
- globalna deduplikacja: PASS
- usunięcie źródła bez kasowania artykułów: PASS
- migracja bazy V14 bez utraty starego rekordu: PASS
- zajęty port 8765 -> automatycznie 8766: PASS
- /api/status /api/config /api/sources: PASS
- bash -n START_ANDROID.sh / INSTALUJ_ANDROID.sh: PASS

WERSJA: 15.0-etap1-multisource-clean
