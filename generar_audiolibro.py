#!/usr/bin/env python3
"""
Genera un audiolibro a partir de un EPUB usando epub_to_audiobook + Piper TTS local.

Añade sobre epub_to_audiobook:
- Velocidad y pausa entre frases con las mismas unidades que la app (x, ms).
- Selección de voz/locutor Piper.
- Tono (pitch) opcional vía postproceso (rubberband) — Piper no lo soporta de forma nativa.
- Filtro de corte de graves opcional (igual que el ajuste "quitar graves" de la app).
- Fusión de capítulos "stub" (portada, separadores de parte) con el capítulo siguiente.
- Agrupado configurable de capítulos en un único archivo final (por nº de capítulos o por
  minutos objetivo), para libros con capítulos muy cortos.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import wave
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
E2A_DIR = HERE / "epub_to_audiobook"
E2A_VENV_PY = E2A_DIR / "venv" / "bin" / "python"
PIPER_BIN = HERE / "piper" / "piper"
VOICES_DIR = HERE / "piper" / "espeak-ng-data" / "voices"

# Chars/sec calibrated from a quick real synthesis sample at speed=1.0 with davefx-medium.
CHARS_PER_SEC_AT_SPEED_1 = 20.0
STUB_CHAR_THRESHOLD_DEFAULT = 400


WORK_ROOT = HERE / ".trabajo"


def work_dir_for(epub: Path, args) -> Path:
    """Carpeta de trabajo estable para un libro y unos ajustes concretos: así, si el proceso se
    interrumpe, al volver a ejecutarlo se encuentra el trabajo ya hecho sin necesitar flags."""
    key = f"{epub.resolve()}|{args.voice}|{args.speed}|{args.pause_ms}|{args.stub_chars}"
    return WORK_ROOT / f"{epub.stem[:50]}_{hashlib.sha1(key.encode()).hexdigest()[:8]}"


def wav_is_complete(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as w:
            n = w.getnframes()
            return len(w.readframes(n)) == n * w.getsampwidth() * w.getnchannels() and n > 0
    except Exception:
        return False


def epub_title_author(epub: Path) -> tuple[str, str]:
    """Lee título y autor de los metadatos Dublin Core del OPF; si faltan, usa el nombre del archivo."""
    title = author = ""
    try:
        with zipfile.ZipFile(epub) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            opf_path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
            opf = ET.fromstring(z.read(opf_path))
            for e in opf.iter():
                text = (e.text or "").strip()
                if e.tag.endswith("}title") and text and not title:
                    title = text
                elif e.tag.endswith("}creator") and text and not author:
                    author = text
    except Exception:
        pass
    return title or epub.stem, author


def book_folder_name(epub: Path) -> str:
    title, author = epub_title_author(epub)
    name = sanitize_filename_part(title, 100)
    return f"{name} - {sanitize_filename_part(author, 60)}" if author else name


def die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def ensure_voice(voice: str) -> Path:
    """Downloads the Piper voice model + json sidecar from HuggingFace if not already present."""
    onnx = VOICES_DIR / f"{voice}.onnx"
    cfg = VOICES_DIR / f"{voice}.onnx.json"
    if onnx.exists() and cfg.exists():
        return onnx
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    lang_short, _, rest = voice.partition("_")
    # voice format: <lang>_<REGION>-<name>-<quality>
    try:
        locale, name, quality = voice.split("-")
        lang = locale.split("_")[0]
    except ValueError:
        die(f"Nombre de voz con formato inesperado: {voice} (se esperaba p.ej. es_ES-davefx-medium)")
    base = f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{lang}/{locale}/{name}/{quality}"
    print(f"Descargando voz {voice}...")
    subprocess.run(["curl", "-sL", "-o", str(onnx), f"{base}/{voice}.onnx"], check=True)
    subprocess.run(["curl", "-sL", "-o", str(cfg), f"{base}/{voice}.onnx.json"], check=True)
    return onnx


def run_e2a(epub: Path, out_dir: Path, voice: str, length_scale: float, sentence_silence: float, workers: int, preview: bool, chapter_start: int, chapter_end: int) -> None:
    cmd = [
        str(E2A_VENV_PY), str(E2A_DIR / "main.py"),
        str(epub), str(out_dir),
        "--tts", "piper",
        "--piper_path", str(PIPER_BIN),
        "--model_name", voice,
        "--piper_length_scale", str(length_scale),
        "--piper_sentence_silence", str(sentence_silence),
        "--output_format", "wav",
        "--worker_count", str(workers),
        "--no_prompt",
        "--language", "es",
        "--output_text",
        "--chapter_start", str(chapter_start),
        "--chapter_end", str(chapter_end),
    ]
    if preview:
        cmd.append("--preview")  # skips actual TTS synthesis, only writes chapter .txt files
    subprocess.run(cmd, check=True, cwd=str(E2A_DIR))


def chapter_files(out_dir: Path, preview: bool) -> list[Path]:
    return sorted(out_dir.glob("*.txt" if preview else "*.wav"))


def char_count_for(txt_path: Path) -> int:
    return len(txt_path.read_text(encoding="utf-8", errors="ignore")) if txt_path.exists() else 0


def chapter_title(wav: Path) -> str:
    """Recovers a human-readable chapter title from epub_to_audiobook's filename, which is
    already '<idx>_<title-with-spaces-as-underscores>' (title = heading tag if the chapter had
    one, else the chapter's first ~60 characters)."""
    stem = wav.stem
    rest = stem.split("_", 1)[1] if "_" in stem and stem.split("_", 1)[0].isdigit() else stem
    rest = re.sub(r"_[0-9a-f]{8}$", "", rest)  # collision hash appended on truncation, if any
    title = rest.replace("_", " ")
    title = re.sub(r"\s+", " ", title).strip()
    return title or "Capítulo"


def sanitize_filename_part(s: str, max_len: int = 80) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "-", s)
    s = re.sub(r"\s+", " ", s).strip(" .-")
    return s[:max_len].strip(" .-") or "Capítulo"


def plan_groups(wavs: list[Path], out_dir: Path, mode: str, value: float, stub_threshold: int, speed: float) -> list[list[Path]]:
    chars = []
    for w in wavs:
        txt = out_dir / (w.stem + ".txt")
        chars.append(char_count_for(txt))

    # Pass 1: merge stub chapters (front matter, part dividers) into the following real chapter.
    merged: list[list[Path]] = []
    merged_chars: list[int] = []
    pending: list[Path] = []
    pending_chars = 0
    for w, c in zip(wavs, chars):
        pending.append(w)
        pending_chars += c
        if c >= stub_threshold or w is wavs[-1]:
            merged.append(pending)
            merged_chars.append(pending_chars)
            pending, pending_chars = [], 0
    if pending:  # trailing stub(s) with no following real chapter: attach to the last group
        if merged:
            merged[-1].extend(pending)
            merged_chars[-1] += pending_chars
        else:
            merged.append(pending)
            merged_chars.append(pending_chars)
    # The last chapter always closes its own group above even if it's itself a stub (e.g. a
    # short "Notas" section) — fold it backward into the previous group instead of leaving a
    # standalone near-silent file.
    if len(merged) > 1 and merged_chars[-1] < stub_threshold:
        merged[-2].extend(merged[-1])
        merged_chars[-2] += merged_chars[-1]
        merged.pop()
        merged_chars.pop()

    if mode == "none":
        return merged

    if mode == "single":
        everything: list[Path] = []
        for g in merged:
            everything.extend(g)
        return [everything]

    groups: list[list[Path]] = []
    if mode == "chapters":
        n = int(value)
        for i in range(0, len(merged), n):
            group: list[Path] = []
            for g in merged[i:i + n]:
                group.extend(g)
            groups.append(group)
        return groups

    if mode == "minutes":
        target_secs = value * 60
        secs_per_char = 1.0 / (CHARS_PER_SEC_AT_SPEED_1 * speed)
        cur: list[Path] = []
        cur_secs = 0.0
        for g, c in zip(merged, merged_chars):
            cur.extend(g)
            cur_secs += c * secs_per_char
            if cur_secs >= target_secs:
                groups.append(cur)
                cur, cur_secs = [], 0.0
        if cur:
            groups.append(cur)
        return groups

    die(f"Modo de agrupado desconocido: {mode}")
    return []


def build_ffmpeg_filters(bass_cut: bool, pitch_semitones: float) -> str:
    filters = []
    if bass_cut:
        filters.append("highpass=f=900")
    if abs(pitch_semitones) > 1e-6:
        pitch_ratio = 2 ** (pitch_semitones / 12)
        filters.append(f"rubberband=pitch={pitch_ratio}")
    return ",".join(filters) if filters else "anull"


def render_group(group: list[Path], out_file: Path, filters: str, bitrate: str, tag_title: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for w in group:
            f.write(f"file '{w.resolve()}'\n")
        list_path = f.name
    try:
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-af", filters,
            "-b:a", bitrate,
            "-metadata", f"title={tag_title}",
            str(out_file),
        ], check=True)
    finally:
        Path(list_path).unlink(missing_ok=True)


def process_epub(epub: Path, args) -> None:
    """Runs the full epub->mp3 pipeline for a single book (same logic as before, factored out
    so it can be called once per file when running in folder-batch mode)."""
    workers = args.workers or 2
    length_scale = 1.0 / args.speed
    sentence_silence = args.pause_ms / 1000.0

    ensure_voice(args.voice)

    if args.resume_from_dir:
        chapters_dir = args.resume_from_dir.resolve()
    elif args.plan_only:
        chapters_dir = Path(tempfile.mkdtemp(prefix="e2a_plan_"))
    else:
        chapters_dir = work_dir_for(epub, args)
        chapters_dir.mkdir(parents=True, exist_ok=True)
    cleanup = args.resume_from_dir is None
    ok = False
    try:
        chapter_start = args.chapter_start
        if not args.plan_only:
            existing = sorted(chapters_dir.glob("*.wav"))
            valid_idx = []
            for f in existing:
                if wav_is_complete(f):
                    valid_idx.append(int(f.stem.split("_")[0]))
                else:
                    f.unlink()  # escritura cortada por la interrupción
                    (chapters_dir / (f.stem + ".txt")).unlink(missing_ok=True)
            if valid_idx:
                # Los capítulos se sintetizan en paralelo y en orden de envío, así que como mucho
                # los últimos `workers` anteriores al mayor índice pueden faltar: se rehacen.
                chapter_start = max(chapter_start, max(valid_idx) - workers + 1)
                print(f"Reanudando: {len(valid_idx)} capítulo(s) ya generado(s) en {chapters_dir}, continuando desde el capítulo {chapter_start}.")

        if args.plan_only:
            print("Analizando capítulos (sin sintetizar audio)...")
            run_e2a(epub, chapters_dir, args.voice, length_scale, sentence_silence, workers, preview=True, chapter_start=chapter_start, chapter_end=args.chapter_end)
        else:
            print(f"Sintetizando capítulos con Piper ({args.voice}, velocidad {args.speed}x, pausa {args.pause_ms}ms, {workers} en paralelo)...")
            run_e2a(epub, chapters_dir, args.voice, length_scale, sentence_silence, workers, preview=False, chapter_start=chapter_start, chapter_end=args.chapter_end)

        wavs = chapter_files(chapters_dir, preview=args.plan_only)
        if not wavs:
            die("epub_to_audiobook no generó ningún capítulo")

        groups = plan_groups(wavs, chapters_dir, args.group_mode, args.group_value, args.stub_chars, args.speed)

        print(f"\nPlan final: {len(groups)} archivo(s) de audio a partir de {len(wavs)} capítulos detectados:")
        for i, g in enumerate(groups, 1):
            names = ", ".join(w.stem for w in g)
            print(f"  {i:02d}: {names}")

        if args.plan_only:
            ok = True
            return

        out_dir = args.output / book_folder_name(epub)
        out_dir.mkdir(parents=True, exist_ok=True)
        filters = build_ffmpeg_filters(args.bass_cut, args.pitch)
        book_stem = epub.stem
        for i, g in enumerate(groups, 1):
            rep = max(g, key=lambda w: char_count_for(chapters_dir / (w.stem + ".txt")))
            title = chapter_title(rep)
            if len(groups) > 1:
                out_name = f"{book_stem} - {i:02d} - {sanitize_filename_part(title)}.mp3"
                tag_title = f"{i}: {title}"
            else:
                out_name = f"{book_stem}.mp3"
                tag_title = book_stem
            out_file = out_dir / out_name
            print(f"Generando {out_file} ({len(g)} capítulo(s))...")
            render_group(g, out_file, filters, args.bitrate, tag_title)

        if args.keep_intermediates:
            dest = out_dir / "capitulos_originales"
            shutil.copytree(chapters_dir, dest, dirs_exist_ok=True)
            print(f"WAVs originales por capítulo copiados a: {dest}")
        ok = True
    finally:
        # Solo se borra el trabajo intermedio si terminó bien (o era un plan); si falló o se
        # interrumpió, se conserva para reanudar en la siguiente ejecución.
        if cleanup and (ok or args.plan_only):
            shutil.rmtree(chapters_dir, ignore_errors=True)


def run_batch(input_dir: Path, args) -> None:
    """Modo carpeta: procesa cada EPUB de input_dir uno tras otro, borrando el EPUB en cuanto
    termina con éxito, para poder ir dejando libros caer en la carpeta mientras corre."""
    input_dir.mkdir(parents=True, exist_ok=True)
    while True:
        epubs = sorted(input_dir.glob("*.epub"))
        if not epubs:
            print(f"No hay EPUBs pendientes en {input_dir}.")
            return
        epub = epubs[0]
        print(f"\n=== Procesando {epub.name} ({len(epubs)} pendiente(s)) ===")
        try:
            process_epub(epub, args)
        except Exception as e:
            print(f"Error procesando {epub.name}: {e}", file=sys.stderr)
            print(f"Se deja {epub.name} en {input_dir} para revisión manual (no se borra).", file=sys.stderr)
            return
        epub.unlink()
        print(f"Eliminado {epub.name} de {input_dir}.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("epub", type=Path, nargs="?", default=None,
                    help="EPUB a convertir. Si se omite, se procesan en orden todos los EPUB de --input "
                         "(por defecto la carpeta 'entrada'), borrando cada uno al terminar.")
    p.add_argument("--input", type=Path, default=Path("entrada"),
                    help="Carpeta con EPUBs a procesar en modo por lotes (cuando no se indica un EPUB)")
    p.add_argument("--output", type=Path, default=Path("salida"))
    p.add_argument("--voice", default="es_ES-davefx-medium")
    p.add_argument("--speed", type=float, default=1.0, help="1.0 = normal, 2.0 = doble de rápido")
    p.add_argument("--pause-ms", type=int, default=200, help="Silencio tras cada frase, en ms")
    p.add_argument("--pitch", type=float, default=0.0, help="Semitonos de cambio de tono (postproceso, opcional)")
    p.add_argument("--bass-cut", action="store_true", help="Filtro de corte de graves (<=900Hz), como en la app")
    p.add_argument("--group-mode", choices=["none", "chapters", "minutes", "single"], default="none",
                    help="none = un archivo por capítulo real; chapters = juntar cada N capítulos; minutes = juntar hasta alcanzar N minutos; single = un único archivo con todo el libro")
    p.add_argument("--group-value", type=float, default=3, help="N para --group-mode chapters/minutes")
    p.add_argument("--stub-chars", type=int, default=STUB_CHAR_THRESHOLD_DEFAULT,
                    help="Capítulos con menos caracteres que esto (portada, separadores de parte) se fusionan con el siguiente")
    p.add_argument("--workers", type=int, default=0,
                    help="Capítulos en paralelo (0 = automático: núcleos/4, ya que cada proceso de Piper usa ~4 núcleos internamente)")
    p.add_argument("--bitrate", default="128k")
    p.add_argument("--keep-intermediates", action="store_true")
    p.add_argument("--plan-only", action="store_true", help="Solo mostrar el plan de agrupado, sin sintetizar")
    p.add_argument("--chapter-start", type=int, default=1)
    p.add_argument("--chapter-end", type=int, default=-1)
    p.add_argument("--resume-from-dir", type=Path, default=None,
                    help="Carpeta con .wav/.txt de capítulos ya generados (misma voz/ajustes) a reutilizar en vez de resintetizar")
    args = p.parse_args()

    args.output = args.output.resolve()

    if args.epub is None:
        run_batch(args.input.resolve(), args)
        print(f"\nListo. Archivos en: {args.output}")
        return

    if not args.epub.exists():
        die(f"No existe el EPUB: {args.epub}")
    args.epub = args.epub.resolve()
    process_epub(args.epub, args)
    print(f"\nListo. Archivos en: {args.output}")


if __name__ == "__main__":
    main()
