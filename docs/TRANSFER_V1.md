# TRANSFER v1 — projekt kontraktu

Status: specyfikacja do implementacji; aplikacja V15.0 jeszcze jej nie obsługuje.

Nazwa: Przewijak_ARTYKULY_TRANSFER_<TRANSFER_ID>.zip.
Zawartość: TRANSFER_MANIFEST.json, database/articles.sqlite3 i archive/ z plikami artykułów.

Manifest zawiera: product=PRZEWIJAK_ARTYKULY, format_version=1, transfer_id, created_at w ISO 8601 ze strefą, producer_platform, app_version, core_version, core_sha256, schema_version, schema_sha256, article_count oraz files (względna ścieżka, rozmiar, SHA-256).

ARTICLE_UID jest identyfikatorem logicznym artykułu. TRANSFER_ID identyfikuje paczkę. Nie zależą od katalogu urządzenia. Tabela aliasów zachowuje źródłowe adresy. content_hash rozpoznaje identyczną treść, lecz nie usuwa pochodzenia z różnych źródeł.

Importer nie uruchamia kodu z ZIP, odrzuca ścieżki absolutne, .., dowiązania, duplikaty nazw i przekroczenie limitów. Ścieżki archiwum są względne, a ścieżki docelowe wylicza adapter urządzenia. Nie importować ustawień systemowych, haseł, ciasteczek ani lokalnych uprawnień.

Wersja SQLite i schemat muszą być jawnie sprawdzane. Niezgodność wymaga obsługiwanej migracji; brak migracji oznacza odmowę importu. Manifest i hashe wykrywają uszkodzenie, nie potwierdzają zaufanego autora paczki.
