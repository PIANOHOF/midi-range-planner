#!/usr/bin/env python3
"""Read-only keyboard-range planning from Standard MIDI note-number events.

Mido decodes MIDI events. Only SMF container boundaries are checked here.
No playing, ports, networking, MIDI rewrite, hand assignment or octave folding.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import io
import json
import os
import stat
import struct
import sys

MAX_BYTES = 8 * 1024 * 1024
MAX_TRACK_BYTES = 64 * 1024
MAX_TRACKS = 256
MAX_TRACK_MESSAGES = 25_000
MAX_MESSAGES = 200_000
MAX_NOTE_EVENTS = 100_000
MAX_TICK = (1 << 53) - 1
PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


class RangeReportError(ValueError):
    """Readable input or option failure; no partial JSON should be emitted."""


def note_label(note: int) -> str:
    if type(note) is not int or not 0 <= note <= 127:
        raise RangeReportError("MIDI-Notennummer muss zwischen 0 und 127 liegen.")
    return f"{PITCH_CLASSES[note % 12]}{note // 12 - 1}"


def _note_reference(note: int) -> dict:
    return {"midi_note": note, "label": note_label(note)}


def _bounded_tracks(data: bytes) -> tuple[int, int, list[bytes]]:
    """Validate container framing, not MIDI event encoding."""
    if len(data) > MAX_BYTES:
        raise RangeReportError("Datei überschreitet 8 MiB.")
    if len(data) < 14 or data[:4] != b"MThd":
        raise RangeReportError("Ein vollständiger Standard-MIDI-Header fehlt.")
    if struct.unpack(">I", data[4:8])[0] != 6:
        raise RangeReportError("Nur ein Standard-MIDI-Header mit sechs Nutzbytes wird unterstützt.")
    kind, count, division = struct.unpack(">HHH", data[8:14])
    if kind not in (0, 1):
        raise RangeReportError("Unterstützt werden SMF Typ 0/1; asynchroner Typ 2 ist ausgeschlossen.")
    if not 1 <= count <= MAX_TRACKS:
        raise RangeReportError(f"Unterstützt werden 1 bis {MAX_TRACKS} Spuren.")
    if kind == 0 and count != 1:
        raise RangeReportError("SMF Typ 0 muss genau eine Spur enthalten.")
    if division == 0 or division & 0x8000:
        raise RangeReportError("Unterstützt wird positive PPQN-Zeitteilung, kein SMPTE.")
    offset = 14
    tracks = []
    for index in range(count):
        if offset + 8 > len(data) or data[offset:offset + 4] != b"MTrk":
            raise RangeReportError(f"Spur {index}: vollständiger MTrk-Block fehlt.")
        length = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        if length > MAX_TRACK_BYTES:
            raise RangeReportError(f"Spur {index} überschreitet 64 KiB Rohdaten.")
        start, end = offset + 8, offset + 8 + length
        if end > len(data):
            raise RangeReportError(f"Spur {index}: Chunk-Länge reicht über das Dateiende hinaus.")
        tracks.append(data[start:end])
        offset = end
    if offset != len(data):
        raise RangeReportError("Zusätzliche nicht deklarierte Bytes oder Blöcke am Dateiende.")
    return kind, division, tracks


def _read_notes(data: bytes) -> dict:
    kind, division, chunks = _bounded_tracks(data)
    try:
        import mido
    except ImportError as exc:
        raise RangeReportError("Mido fehlt; bitte requirements.txt in einer virtuellen Umgebung installieren.") from exc
    notes = []
    tracks = []
    counts = Counter()
    for track_index, chunk in enumerate(chunks):
        framed = struct.pack(">4sIHHH4sI", b"MThd", 6, 0, 1, division, b"MTrk", len(chunk)) + chunk
        try:
            track = mido.MidiFile(file=io.BytesIO(framed), clip=False).tracks[0]
        except (EOFError, OSError, ValueError, TypeError, IndexError, KeyError, struct.error, mido.KeySignatureError) as exc:
            raise RangeReportError(f"Spur {track_index}: ungültige MIDI-Daten ({type(exc).__name__}).") from exc
        if len(track) > MAX_TRACK_MESSAGES:
            raise RangeReportError(f"Spur {track_index} überschreitet {MAX_TRACK_MESSAGES} Nachrichten.")
        counts["messages"] += len(track)
        if counts["messages"] > MAX_MESSAGES:
            raise RangeReportError(f"Datei überschreitet {MAX_MESSAGES} Nachrichten insgesamt.")
        if not track or track[-1].type != "end_of_track" or any(message.type == "end_of_track" for message in track[:-1]):
            raise RangeReportError(f"Spur {track_index}: genau eine abschließende End-of-Track-Nachricht wird benötigt.")
        tick = 0
        track_notes = 0
        for message_index, message in enumerate(track):
            tick += message.time
            if tick > MAX_TICK:
                raise RangeReportError("Absolute Ticks überschreiten den sicheren JSON-Ganzzahlbereich.")
            if message.type == "midi_port":
                raise RangeReportError("MIDI-Port-Metadaten werden nicht unterstützt; Kanalgruppen wären ohne Portmodell irreführend.")
            if message.type == "note_on" and message.velocity > 0:
                notes.append({"midi_note": message.note, "channel": message.channel + 1,
                              "track_index": track_index, "message_index": message_index, "absolute_tick": tick})
                track_notes += 1
                if len(notes) > MAX_NOTE_EVENTS:
                    raise RangeReportError(f"Datei überschreitet {MAX_NOTE_EVENTS} positive Note-On-Ereignisse.")
            elif message.type == "note_on":
                counts["zero_velocity_note_on"] += 1
            elif message.type == "note_off":
                counts["note_off"] += 1
            elif message.type == "pitchwheel":
                counts["pitchwheel"] += 1
            elif message.type == "sysex":
                counts["sysex"] += 1
        tracks.append({"track_index": track_index, "message_count": len(track), "positive_note_on_count": track_notes, "end_tick": tick})
        del track
    notes.sort(key=lambda event: (event["absolute_tick"], event["track_index"], event["message_index"]))
    return {"smf_type": kind, "ticks_per_quarter_note": division, "tracks": tracks, "counts": counts, "notes": notes}


def _int_option(value: int, name: str, low: int, high: int) -> None:
    if type(value) is not int or not low <= value <= high:
        raise RangeReportError(f"{name} muss eine ganze Zahl zwischen {low} und {high} sein.")


def _selection(values, name: str, low: int, high: int) -> list[int] | None:
    if values is None:
        return None
    if not isinstance(values, (list, tuple, set)) or not values:
        raise RangeReportError(f"{name} muss eine nicht leere Liste ganzer Zahlen sein.")
    for value in values:
        _int_option(value, name, low, high)
    if len(values) != len(set(values)):
        raise RangeReportError(f"{name} enthält doppelte Einträge.")
    return sorted(values)


def _summary(events: list[dict]) -> dict:
    histogram = Counter(event["midi_note"] for event in events)
    if not histogram:
        return {"note_on_count": 0, "distinct_note_count": 0, "lowest": None, "highest": None, "span_keys": 0}
    low, high = min(histogram), max(histogram)
    return {"note_on_count": len(events), "distinct_note_count": len(histogram), "lowest": _note_reference(low),
            "highest": _note_reference(high), "span_keys": high - low + 1}


def analyze_bytes(data: bytes, *, lowest: int, highest: int, channels=None, tracks=None,
                  shift_min: int = -24, shift_max: int = 24, example_limit: int = 8) -> dict:
    """Plan uniform shifts of selected raw note numbers; never rewrite the MIDI."""
    _int_option(lowest, "Untergrenze", 0, 127)
    _int_option(highest, "Obergrenze", 0, 127)
    if lowest > highest:
        raise RangeReportError("Untergrenze darf nicht über der Obergrenze liegen.")
    _int_option(shift_min, "Kleinste Verschiebung", -127, 127)
    _int_option(shift_max, "Größte Verschiebung", -127, 127)
    if shift_min > shift_max:
        raise RangeReportError("Das Suchfenster für Verschiebungen ist umgekehrt.")
    _int_option(example_limit, "Beispiellimit", 0, 32)
    selected_channels = _selection(channels, "Kanalauswahl", 1, 16)
    selected_tracks = _selection(tracks, "Spurauswahl", 0, MAX_TRACKS - 1)
    raw = _read_notes(data)
    track_count = len(raw["tracks"])
    if selected_tracks is not None and any(index >= track_count for index in selected_tracks):
        raise RangeReportError("Eine ausgewählte Spur existiert in dieser Datei nicht.")
    channel_set = set(selected_channels if selected_channels is not None else range(1, 17))
    track_set = set(selected_tracks if selected_tracks is not None else range(track_count))
    selected = [event for event in raw["notes"] if event["track_index"] in track_set and event["channel"] in channel_set]
    summary = _summary(selected)
    histogram = Counter(event["midi_note"] for event in selected)
    by_track, by_channel = defaultdict(list), defaultdict(list)
    for event in selected:
        by_track[event["track_index"]].append(event)
        by_channel[event["channel"]].append(event)
    outside = [event for event in selected if not lowest <= event["midi_note"] <= highest]
    outside_histogram = Counter(event["midi_note"] for event in outside)
    theoretical_low = theoretical_high = chosen = None
    possible = []
    if selected:
        theoretical_low = lowest - min(histogram)
        theoretical_high = highest - max(histogram)
        possible = list(range(max(shift_min, theoretical_low), min(shift_max, theoretical_high) + 1))
        if possible:
            chosen = min(possible, key=lambda shift: (abs(shift), shift))
            status = "uniform_fit_found"
        else:
            status = "keyboard_span_too_small" if theoretical_low > theoretical_high else "no_fit_in_search_window"
    else:
        status = "no_selected_note_on_events"
    return {
        "schema_version": 1,
        "input": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        "scope": "Raw MIDI note-number coverage for an explicitly chosen chromatic keyboard range, not sounding pitch or playability.",
        "note_label_convention": "MIDI60=C4; international sharp names; B means German H. MIDI numbers are authoritative.",
        "smf_type": raw["smf_type"], "ticks_per_quarter_note": raw["ticks_per_quarter_note"],
        "keyboard": {"lowest": _note_reference(lowest), "highest": _note_reference(highest), "keys_inclusive": highest - lowest + 1},
        "selection": {"tracks_zero_based": sorted(track_set), "channels_one_based": sorted(channel_set),
                      "positive_note_on_count_in_file": len(raw["notes"]), "selected_note_on_count": len(selected),
                      "excluded_positive_note_on_count": len(raw["notes"]) - len(selected)},
        "observed": summary,
        "tracks": [{**track, "selected": _summary(by_track[track["track_index"]])} for track in raw["tracks"]],
        "channels": [{"channel": channel, **_summary(by_channel[channel])} for channel in sorted(by_channel)],
        "note_histogram": [{**_note_reference(note), "note_on_count": histogram[note]} for note in sorted(histogram)],
        "outside_original_keyboard": {"note_on_count": len(outside),
            "notes": [{**_note_reference(note), "note_on_count": outside_histogram[note]} for note in sorted(outside_histogram)],
            "first_examples": outside[:example_limit], "example_limit": example_limit,
            "example_order": "absolute_tick, track_index, message_index for lookup only; no cross-track performance-order claim"},
        "uniform_shift_plan": {"status": status, "search_min_semitones": shift_min, "search_max_semitones": shift_max,
            "theoretical_min_semitones": theoretical_low, "theoretical_max_semitones": theoretical_high,
            "all_fitting_semitones_in_search": possible, "octave_preserving_fitting_semitones": [shift for shift in possible if shift % 12 == 0],
            "smallest_absolute_fit_semitones": chosen, "tie_break": "negative before positive; mathematical convention, not a musical recommendation",
            "mapping_for_smallest_fit": [{"original": _note_reference(note), "mapped": _note_reference(note + chosen),
                                          "note_on_count": histogram[note]} for note in sorted(histogram)] if chosen is not None else []},
        "message_counts": {key: raw["counts"][key] for key in ["messages", "zero_velocity_note_on", "note_off", "pitchwheel", "sysex"]},
        "warnings": (["Kanal10 ist mitgezählt. Eine Instrumenten- oder Schlagzeugrolle wird nicht aus der Kanalnummer erkannt."] if 10 in by_channel else []) + [
            "Nur Note On mit Velocity größer null zählt als Anschlag; Velocity0 und Note Off erweitern den Tonumfang nicht.",
            "Pitch Bend, Stimmung, SysEx, Klangprogramme, Tastaturzonen und geräteeigene Transposition werden nicht interpretiert.",
            "Ein passender Notennummernbereich beweist weder bequeme Handlage, Fingersatz, erreichbare Akkorde noch musikalisch passende Tonart.",
            "Keine Noten werden gelöscht, oktaviert, gespeichert, abgespielt oder an ein Gerät gesendet. Ticks sind Suchpositionen, keine Takt- oder Sekundenangaben."]
    }


def analyze_file(path, **options) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise RangeReportError("Eingabe muss eine reguläre Datei sein; kein Gerät, FIFO oder Verzeichnis.")
            if info.st_size > MAX_BYTES:
                raise RangeReportError("Datei überschreitet 8 MiB.")
            data = source.read(MAX_BYTES + 1)
    except RangeReportError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise RangeReportError(f"Datei kann nicht gelesen werden ({type(exc).__name__}).") from exc
    return analyze_bytes(data, **options)


def _cli_list(value: str) -> list[int]:
    try:
        if len(value) > 1024 or not value:
            raise ValueError
        return [int(part) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Kommagetrennte ganze Zahlen erwartet, z.B.1,2.") from exc


def main(argv=None) -> int:
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description="Lokaler MIDI-Tonumfangsbericht und einheitliche Verschiebungsplanung; schreibt keine MIDI-Datei.")
    parser.add_argument("midi_file")
    parser.add_argument("--lowest", type=int, required=True, help="Unterste tatsächlich verfügbare MIDI-Notennummer, einschließlich.")
    parser.add_argument("--highest", type=int, required=True, help="Oberste tatsächlich verfügbare MIDI-Notennummer, einschließlich.")
    parser.add_argument("--channels", type=_cli_list, help="MIDI-Kanäle1..16, z.B.1,2; standardmäßig alle.")
    parser.add_argument("--tracks", type=_cli_list, help="Spurindizes ab0, z.B.1,2; standardmäßig alle.")
    parser.add_argument("--shift-min", type=int, default=-24)
    parser.add_argument("--shift-max", type=int, default=24)
    parser.add_argument("--example-limit", type=int, default=8)
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        print("Fehler: Python3.10 oder neuer wird benötigt.", file=sys.stderr)
        return 2
    options = vars(args).copy()
    path = options.pop("midi_file")
    try:
        report = analyze_file(path, **options)
    except RangeReportError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
