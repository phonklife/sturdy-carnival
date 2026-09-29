# Suno-Archive-Automator

Zautomatyzowane archiwum motywów muzycznych oparte na imporcie danych źródłowych i zapisie metadanych do lokalnej bazy SQLite.

## Co robi aktualna wersja

Skrypt `generator.py`:

- pobiera dane utworów z pliku JSON albo z endpointu HTTP(S),
- normalizuje podstawowe metadane utworu,
- zapisuje rekordy do bazy SQLite,
- aktualizuje istniejące rekordy po `source_id`.

## Wymagany format danych

Źródło może być:

- tablicą JSON:

```json
[
  {
    "id": "song-001",
    "title": "Solar Echo",
    "artist": "virtualluser",
    "tags": ["ambient", "synthwave"],
    "url": "https://example.com/tracks/song-001",
    "audio_url": "https://example.com/audio/song-001.mp3"
  }
]
```

- albo obiektem zawierającym listę pod kluczem `tracks`:

```json
{
  "tracks": [
    {
      "id": "song-001",
      "title": "Solar Echo"
    }
  ]
}
```

Obsługiwane pola:

- `id` / `track_id` / `slug` / `url` — identyfikator źródłowy,
- `title` / `name` — tytuł,
- `artist` — wykonawca,
- `url` / `source_url` — link źródłowy,
- `audio_url` / `download_url` — link do audio,
- `tags` — lista tagów lub string rozdzielony przecinkami,
- `status` albo `archived`,
- `archived_at`.

Jeśli rekord zawiera `archived_at`, skrypt zapisze jego status jako `archived` nawet wtedy, gdy źródło przekaże konfliktującą wartość w polu `status`.

## Użycie

Import z pliku:

```bash
python generator.py --source-file source.json --database archive.db
```

Import z API:

```bash
python generator.py --source-url https://example.com/tracks.json --database archive.db
```

Opcjonalne parametry:

- `--root-key` — nazwa klucza z listą rekordów, domyślnie `tracks`,
- `--default-artist` — domyślny wykonawca, gdy rekord nie zawiera `artist`.

## Zasady aktualizacji rekordów

- `artist`, `status`, `source_url`, `audio_url`, `archived_at` i `tags` pozostają bez zmian, jeśli pole nie występuje w aktualizacji.
- jeśli `source_url`, `audio_url` albo `archived_at` zostanie przekazane jawnie jako pusty string albo `null`, zapis w bazie zostanie wyczyszczony,
- jeśli `tags` zostanie przekazane jawnie jako pusta lista, zapisane tagi zostaną wyczyszczone,
- jeśli rekord zawiera `archived_at`, status zostanie ustawiony na `archived`,
- poprawne rekordy są zapisywane nawet wtedy, gdy późniejszy rekord z tej samej partii okaże się niepoprawny.

## Testy

```bash
python -m unittest discover -s tests
```
