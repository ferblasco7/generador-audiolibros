# audiolibros

Convierte EPUBs a audiolibros MP3 usando `epub_to_audiobook` + Piper TTS local (sin nube, sin coste por libro).

## Uso

Un solo libro:
```
python3 generar_audiolibro.py libro.epub
```

Modo por lotes (sin argumentos): procesa en orden todos los `.epub` que haya en `entrada/`, generando el MP3 en `salida/` y borrando el EPUB de `entrada/` en cuanto termina con éxito. Así se pueden ir dejando caer libros en `entrada/` mientras el script sigue corriendo.
```
python3 generar_audiolibro.py
```
Si un libro falla, se detiene el lote y se deja ese EPUB en `entrada/` sin borrar, para revisión manual.

Opciones relevantes (aplican a ambos modos): `--voice`, `--speed`, `--pause-ms`, `--pitch`, `--bass-cut`, `--group-mode {none,chapters,minutes,single}`, `--group-value`, `--plan-only` (solo mostrar el plan de agrupado sin sintetizar), `--resume-from-dir` (usar una carpeta de capítulos concreta).

**Reanudación automática**: los capítulos se sintetizan en `.trabajo/<libro>_<hash>/` (el hash depende de voz/velocidad/pausa/stub-chars). Si el proceso se interrumpe, basta volver a ejecutar el mismo comando: se descartan los WAV truncados y se continúa desde el siguiente capítulo no generado (rehaciendo como mucho `workers` capítulos previos al mayor índice, por el paralelismo). La carpeta de trabajo se borra solo cuando el libro termina bien.

`--input` cambia la carpeta de entrada (por defecto `entrada/`); solo tiene efecto cuando no se pasa un EPUB como argumento.

## Estructura

- `generar_audiolibro.py`: script principal, envoltorio sobre `epub_to_audiobook/`.
- `epub_to_audiobook/`: motor de síntesis (venv propio en `epub_to_audiobook/venv/`).
- `piper/`: binario y datos de Piper TTS; las voces se descargan a `piper/espeak-ng-data/voices/` la primera vez que se usan.
- `.trabajo/`: capítulos intermedios de libros en curso (se borra al terminar cada libro).
- `entrada/`: EPUBs pendientes de convertir (modo por lotes).
- `salida/`: MP3 generados, cada libro en su carpeta `Título - Autor/` (según los metadatos del EPUB; sin autor, solo el título).
