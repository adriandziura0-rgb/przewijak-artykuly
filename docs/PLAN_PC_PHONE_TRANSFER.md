# Plan rozwoju ARTYKUŁY — wspólny silnik PC/PHONE

## Cel i zasady

PC i PHONE mają korzystać z jednej implementacji pobierania, czyszczenia treści, normalizacji adresów oraz deduplikacji. Każde urządzenie ma własną bazę roboczą. Dane przenosimy wyłącznie przez wersjonowany ZIP TRANSFER, bez podmiany aktywnej bazy SQLite.

Zachować ręczny wybór folderu, artykuły i historię użytkownika. Nie dodawać elementów LIVE eSoccer. Nie przenosić ścieżek Windows na Android ani uprawnień SAF na PC.

## Etap 0 — punkt wyjściowy

Zachowany kod V15.0 MULTISOURCE z najnowszej paczki użytkownika. Źródła i ustawienia są już przechowywane w SQLite; automatyczne cykliczne obchodzenie źródeł pozostaje do implementacji. Przed refaktoryzacją: test startu, wyboru katalogu, zapisu artykułu i rozpoznawania istniejących rekordów. Obecny import archiwum nie jest importem TRANSFER.

## Etap 1 — wydzielenie wspólnego silnika

Wydzielić logikę z app.py do shared_core bez zmiany zachowania pobierania. PC i PHONE importują ten sam kod, a wydania zapisują jego wersję i SHA-256. Adaptery systemowe odpowiadają wyłącznie za okno, tło, foldery, SD i uruchamianie.

Zachować ograniczenia sieciowe: jedno żądanie naraz, co najmniej 5 s odstępu, maksymalnie jedna ponowna próba, cache, zatrzymanie na 403/429 i rozpoznawanie CAPTCHA. Nie omijać blokad.

## Etap 2 — wspólny model artykułu

Obecna tabela articles zawiera canonical, url, title, content_hash, saved_at, output_folder i source. Rozszerzenia wymagają jawnej migracji i kopii bezpieczeństwa.

Stały ARTICLE_UID przypisany raz, zachowywany podczas przenoszenia. Powiązać adresy kanoniczne i źródłowe jako aliasy. Ten sam tekst w różnych redakcjach może mieć wspólny hash, ale musi zachować oba źródła. Aktualizacja artykułu pod tym samym adresem jest nową wersją treści, nie nowym niepowiązanym artykułem. Oddzielić datę publikacji, pobrania i aktualizacji.

## Etap 3 — eksport TRANSFER

Spójny snapshot przez SQLite Backup API oraz kopie plików artykułów. Manifest: TRANSFER_ID, wersja formatu, wersja silnika, odcisk schematu, daty, liczności i SHA-256 każdego pliku. ZIP budować jako plik tymczasowy, sprawdzić i dopiero potem oznaczyć jako gotowy. Każdy eksport ma nową nazwę.

## Etap 4 — import TRANSFER

Sprawdzać ścieżki ZIP, limity rozmiaru i liczby plików, manifest, hashe, integralność SQLite, wersję formatu i zgodność schematu. Odrzucać pliki niezgodne przed modyfikacją danych.

Pokazać podgląd: nowe artykuły, duplikaty, aktualizacje, konflikty. Przed importem wykonać spójną kopię bazy docelowej. Przy aktywnym pobieraniu import zaczeka na zatrzymanie zapisów. Scalać w transakcji, zachowywać ARTICLE_UID i daty. Starszy rekord nie nadpisuje nowszego. Konflikty zachować do rozstrzygnięcia; identyczny hash nie usuwa informacji o źródle. Powtórny TRANSFER_ID nie dodaje duplikatów. Pliki etapować przed zatwierdzeniem; awaria ma pozostawić bazę i archiwum w odzyskiwalnym stanie.

## Etap 5 — aplikacje PC i PHONE oraz monitoring wielu źródeł

Wspólny scheduler obchodzący włączone źródła, z kontrolowanym odstępem GET i stanem osobno dla każdego źródła. STOP na blokadzie danego serwisu ma być jawnie raportowany.

PC: osobne okno aplikacji i build EXE. PHONE: APK korzystający ze wspólnego silnika, systemowy wybór folderu/SD, trwałe uprawnienia i usługa pobierania w tle. Dodać tylko funkcje importu/eksportu potrzebne do wymiany. Nie obiecywać działania po wymuszonym zatrzymaniu aplikacji przez użytkownika.

## Etap 6 — walidacja i wydanie CLEAN

Testy: PC → PHONE → PC; dwie różne bazy z częściowo wspólnymi artykułami; ponowny import; starsza paczka; uszkodzony ZIP; brak miejsca; utrata dostępu SD; awaria w trakcie importu; odłączenie sieci; tło i ponowne otwarcie; TVN24/TVP Info/TV Republika; CAPTCHA/403/429; paginacja i przewijanie.

Warunek wydania: dane i metadane zachowane, brak duplikatów, jednakowy silnik i jawna zgodność formatu. Paczka bez baz użytkownika, .venv, cache i starych wydań. Testy rozwojowe pozostają w repozytorium, artefakt użytkowy zawiera tylko pliki potrzebne do pracy.
