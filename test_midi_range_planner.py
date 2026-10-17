"""Literal SMF byte fixtures and an independent exhaustive transposition oracle."""
import hashlib
import json
import os
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import midi_range_planner as planner

EOT = b"\x00\xff\x2f\x00"


def smf(*tracks, kind=0, division=480):
    return (b"MThd\x00\x00\x00\x06" + struct.pack(">HHH", kind, len(tracks), division)
            + b"".join(b"MTrk" + struct.pack(">I", len(track)) + track for track in tracks))


def note_track(notes, channel=0):
    return b"".join(bytes([0, 0x90 | channel, note, 64]) for note in notes) + EOT


class PlannerTests(unittest.TestCase):
    def analyze(self, notes, **options):
        return planner.analyze_bytes(smf(note_track(notes)), lowest=options.pop("lowest", 36),
                                     highest=options.pop("highest", 96), **options)

    def rejects(self, data, fragment, **options):
        with self.assertRaisesRegex(planner.RangeReportError, fragment):
            planner.analyze_bytes(data, lowest=options.pop("lowest", 36), highest=options.pop("highest", 96), **options)

    def test_range_is_inclusive_not_number_of_distinct_notes(self):
        report = self.analyze([60, 72, 60])
        self.assertEqual(report["observed"]["span_keys"], 13)
        self.assertEqual(report["observed"]["distinct_note_count"], 2)
        self.assertEqual(report["observed"]["note_on_count"], 3)
        self.assertEqual(report["note_histogram"], [{"midi_note": 60, "label": "C4", "note_on_count": 2},
                                                   {"midi_note": 72, "label": "C5", "note_on_count": 1}])

    def test_exact_octave_fit_and_mapping(self):
        report = self.analyze([48, 72, 84], lowest=36, highest=72)
        plan = report["uniform_shift_plan"]
        self.assertEqual(plan["all_fitting_semitones_in_search"], [-12])
        self.assertEqual(plan["octave_preserving_fitting_semitones"], [-12])
        self.assertEqual(plan["smallest_absolute_fit_semitones"], -12)
        self.assertEqual([(m["original"]["midi_note"], m["mapped"]["midi_note"]) for m in plan["mapping_for_smallest_fit"]], [(48, 36), (72, 60), (84, 72)])
        self.assertEqual(report["outside_original_keyboard"]["note_on_count"], 1)
        self.assertEqual(report["outside_original_keyboard"]["notes"][0]["midi_note"], 84)

    def test_already_fits_returns_zero_and_all_other_uniform_choices(self):
        plan = self.analyze([48, 84])["uniform_shift_plan"]
        self.assertEqual(plan["all_fitting_semitones_in_search"], list(range(-12, 13)))
        self.assertEqual(plan["smallest_absolute_fit_semitones"], 0)
        self.assertEqual(plan["octave_preserving_fitting_semitones"], [-12, 0, 12])

    def test_too_wide_cannot_be_fixed_by_uniform_transposition(self):
        report = self.analyze([0, 127], lowest=21, highest=108)
        self.assertEqual(report["observed"]["span_keys"], 128)
        self.assertEqual(report["uniform_shift_plan"]["status"], "keyboard_span_too_small")
        self.assertEqual(report["uniform_shift_plan"]["all_fitting_semitones_in_search"], [])
        self.assertIsNone(report["uniform_shift_plan"]["smallest_absolute_fit_semitones"])
        self.assertEqual(report["uniform_shift_plan"]["mapping_for_smallest_fit"], [])

    def test_search_window_failure_is_distinct_from_keyboard_span_failure(self):
        narrow_search = self.analyze([48, 60], lowest=12, highest=24)
        self.assertEqual(narrow_search["uniform_shift_plan"]["status"], "no_fit_in_search_window")
        self.assertEqual(narrow_search["uniform_shift_plan"]["theoretical_min_semitones"], -36)
        expanded = self.analyze([48, 60], lowest=12, highest=24, shift_min=-48)
        self.assertEqual(expanded["uniform_shift_plan"]["all_fitting_semitones_in_search"], [-36])

    def test_velocity_zero_and_note_off_do_not_expand_observed_range(self):
        track = b"\x00\x90\x3c\x40\x00\x90\x7f\x00\x00\x80\x00\x40" + EOT
        report = planner.analyze_bytes(smf(track), lowest=60, highest=60)
        self.assertEqual(report["observed"]["span_keys"], 1)
        self.assertEqual(report["observed"]["lowest"]["midi_note"], 60)
        self.assertEqual(report["message_counts"]["zero_velocity_note_on"], 1)
        self.assertEqual(report["message_counts"]["note_off"], 1)

    def test_one_key_keyboard_requires_one_exact_note_after_shift(self):
        plan = self.analyze([60, 60], lowest=61, highest=61)["uniform_shift_plan"]
        self.assertEqual(plan["all_fitting_semitones_in_search"], [1])
        self.assertEqual(plan["octave_preserving_fitting_semitones"], [])

    def test_channels_and_tracks_are_distinct_explicit_filters(self):
        data = smf(note_track([48, 84], 0), note_track([36, 60], 1), note_track([35], 9), kind=1)
        all_notes = planner.analyze_bytes(data, lowest=36, highest=72)
        selected = planner.analyze_bytes(data, lowest=36, highest=72, channels=[1], tracks=[0])
        self.assertEqual(all_notes["selection"]["positive_note_on_count_in_file"], 5)
        self.assertEqual(all_notes["uniform_shift_plan"]["status"], "keyboard_span_too_small")
        self.assertEqual(selected["selection"]["selected_note_on_count"], 2)
        self.assertEqual(selected["selection"]["excluded_positive_note_on_count"], 3)
        self.assertEqual(selected["uniform_shift_plan"]["all_fitting_semitones_in_search"], [-12])

    def test_channel_ten_is_not_automatically_removed_or_called_percussion(self):
        report = planner.analyze_bytes(smf(note_track([35], 9)), lowest=0, highest=127)
        self.assertEqual(report["channels"][0]["channel"], 10)
        self.assertEqual(report["observed"]["lowest"]["midi_note"], 35)
        self.assertIn("Kanal10", report["warnings"][0])

    def test_same_channel_across_tracks_is_aggregated_not_split(self):
        data = smf(note_track([40], 0), note_track([80], 0), kind=1)
        report = planner.analyze_bytes(data, lowest=21, highest=108)
        self.assertEqual(len(report["channels"]), 1)
        self.assertEqual(report["channels"][0]["span_keys"], 41)
        self.assertEqual(report["tracks"][0]["selected"]["span_keys"], 1)
        self.assertEqual(report["tracks"][1]["selected"]["span_keys"], 1)

    def test_outside_examples_keep_actual_locations_and_are_bounded(self):
        # First track outlier is later (tick480); second track outlier at0.
        a = b"\x83\x60\x90\x18\x40\x00\x90\x19\x40" + EOT
        b = b"\x00\x91\x78\x40" + EOT
        report = planner.analyze_bytes(smf(a, b, kind=1), lowest=36, highest=96, example_limit=1)
        outside = report["outside_original_keyboard"]
        self.assertEqual(outside["note_on_count"], 3)
        self.assertEqual(outside["first_examples"], [{"midi_note": 120, "channel": 2, "track_index": 1, "message_index": 0, "absolute_tick": 0}])
        none = planner.analyze_bytes(smf(a, b, kind=1), lowest=36, highest=96, example_limit=0)
        self.assertEqual(none["outside_original_keyboard"]["first_examples"], [])

    def test_simultaneous_track_permutations_do_not_change_range_or_plan(self):
        a, b = note_track([40, 64]), note_track([52, 76])
        first = planner.analyze_bytes(smf(a, b, kind=1), lowest=36, highest=72)
        second = planner.analyze_bytes(smf(b, a, kind=1), lowest=36, highest=72)
        self.assertEqual(first["observed"], second["observed"])
        self.assertEqual(first["uniform_shift_plan"], second["uniform_shift_plan"])

    def test_empty_or_filtered_out_notes_do_not_invent_range_or_fit(self):
        for data, options in [(smf(EOT), {}), (smf(note_track([60])), {"channels": [2]})]:
            report = planner.analyze_bytes(data, lowest=36, highest=96, **options)
            self.assertEqual(report["uniform_shift_plan"]["status"], "no_selected_note_on_events")
            self.assertIsNone(report["observed"]["lowest"])
            self.assertEqual(report["uniform_shift_plan"]["all_fitting_semitones_in_search"], [])

    def test_pitch_bend_and_sysex_are_counted_but_not_translated_into_notes(self):
        track = b"\x00\x90\x3c\x40\x00\xe0\x7f\x7f\x00\xf0\x03\x7d\x01\xf7" + EOT
        report = planner.analyze_bytes(smf(track), lowest=60, highest=60)
        self.assertEqual(report["observed"]["lowest"]["midi_note"], 60)
        self.assertEqual(report["message_counts"]["pitchwheel"], 1)
        self.assertEqual(report["message_counts"]["sysex"], 1)

    def test_note_labels_explicitly_choose_c4_60_and_international_b(self):
        self.assertEqual([planner.note_label(n) for n in [0, 21, 59, 60, 108, 127]], ["C-1", "A0", "B3", "C4", "C8", "G9"])
        for invalid in [-1, 128, True, 60.0]:
            with self.assertRaises(planner.RangeReportError): planner.note_label(invalid)

    def test_exhaustive_oracle_over_independent_random_note_sets(self):
        randomizer = random.Random(64012026)
        for _ in range(120):
            notes = [randomizer.randrange(128) for _ in range(randomizer.randrange(1, 15))]
            low, high = sorted([randomizer.randrange(128), randomizer.randrange(128)])
            report = self.analyze(notes, lowest=low, highest=high, shift_min=-127, shift_max=127)
            expected = [shift for shift in range(-127, 128) if all(low <= note + shift <= high for note in notes)]
            self.assertEqual(report["uniform_shift_plan"]["all_fitting_semitones_in_search"], expected)
            for row in report["uniform_shift_plan"]["mapping_for_smallest_fit"]:
                self.assertTrue(low <= row["mapped"]["midi_note"] <= high)

    def test_option_errors_and_nonexistent_tracks_are_clear(self):
        for options, fragment in [({"lowest": 128}, "Untergrenze"), ({"lowest": 80, "highest": 40}, "Untergrenze"),
                                  ({"shift_min": 2, "shift_max": -2}, "Suchfenster"), ({"channels": [0]}, "Kanalauswahl"),
                                  ({"channels": [1, 1]}, "doppelte"), ({"channels": []}, "nicht leere"),
                                  ({"tracks": [1]}, "existiert"), ({"example_limit": 33}, "Beispiellimit")]:
            self.rejects(smf(EOT), fragment, **options)

    def test_running_status_uses_mido(self):
        track = b"\x00\x90\x30\x40\x01\x54\x40\x01\x7f\x00" + EOT
        report = planner.analyze_bytes(smf(track), lowest=36, highest=72)
        self.assertEqual(report["observed"]["span_keys"], 37)
        self.assertEqual(report["uniform_shift_plan"]["all_fitting_semitones_in_search"], [-12])

    def test_types_time_division_port_and_malformed_metadata(self):
        self.rejects(smf(EOT, kind=2), "Typ 2")
        self.rejects(smf(EOT, division=0xE728), "SMPTE")
        self.rejects(smf(EOT, division=0), "PPQN")
        self.rejects(smf(EOT, EOT, kind=0), "genau eine")
        for port in [0, 4]: self.rejects(smf(b"\x00\xff\x21\x01" + bytes([port]) + EOT), "MIDI-Port")
        self.rejects(smf(b"\x00\xff\x59\x02\x08\x00" + EOT), "KeySignatureError")

    def test_header_chunk_event_and_end_of_track_errors(self):
        for data in [b"", b"not-midi", b"MThd\x00\x00\x00\x06"]: self.rejects(data, "Header")
        self.rejects(smf(EOT)[:-1], "Dateiende")
        self.rejects(smf(EOT) + b"extra", "nicht deklarierte")
        self.rejects(smf(b"\x00\x90\x40"), "ungültige MIDI")
        self.rejects(smf(b"\x00\x90\x3c\x80" + EOT), "ungültige MIDI")
        self.rejects(smf(b"\x00\x90\x3c\x40"), "End-of-Track")
        self.rejects(smf(EOT + note_track([60])), "End-of-Track")

    def test_resource_limits(self):
        self.rejects(b"x" * (planner.MAX_BYTES + 1), "8 MiB")
        self.rejects(smf(b"x" * (planner.MAX_TRACK_BYTES + 1)), "64 KiB")
        self.rejects(smf(*([EOT] * (planner.MAX_TRACKS + 1)), kind=1), "256 Spuren")
        with patch.object(planner, "MAX_TRACK_MESSAGES", 1): self.rejects(smf(note_track([60])), "Nachrichten")
        with patch.object(planner, "MAX_MESSAGES", 1): self.rejects(smf(EOT, EOT, kind=1), "insgesamt")
        with patch.object(planner, "MAX_NOTE_EVENTS", 1): self.rejects(smf(note_track([60, 61])), "Note-On")

    def test_actual_running_status_message_limit(self):
        track = b"\x00\xc0\x00" + b"\x00\x00" * planner.MAX_TRACK_MESSAGES + EOT
        self.assertLess(len(track), planner.MAX_TRACK_BYTES)
        self.rejects(smf(track), "Nachrichten")

    def test_input_read_only_and_filename_absent_from_output(self):
        data = smf(note_track([48, 84]))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "private-score.mid"
            source.write_bytes(data)
            mtime = source.stat().st_mtime_ns
            report = planner.analyze_file(source, lowest=36, highest=72)
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(source.stat().st_mtime_ns, mtime)
            self.assertEqual(report["input"]["sha256"], hashlib.sha256(data).hexdigest())
            self.assertNotIn("private-score", json.dumps(report))
            self.assertEqual(list(Path(directory).iterdir()), [source])

    def test_cli_success_and_invalid_arguments_have_no_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "test.mid"
            source.write_bytes(smf(note_track([48, 84])))
            command = [sys.executable, str(Path(planner.__file__)), str(source), "--lowest", "36", "--highest", "72"]
            good = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertEqual(json.loads(good.stdout)["uniform_shift_plan"]["smallest_absolute_fit_semitones"], -12)
            for extra in [["--channels", "x"], ["--channels", "17"], ["--tracks", "2"], ["--highest", "0"]]:
                bad = subprocess.run(command + extra, capture_output=True, text=True)
                self.assertEqual(bad.returncode, 2)
                self.assertEqual(bad.stdout, "")
                self.assertNotIn("Traceback", bad.stderr)

    def test_cli_bad_missing_and_non_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bad.mid"; source.write_bytes(b"not MIDI")
            for value in [source, Path(directory) / "missing.mid", Path(directory)]:
                run = subprocess.run([sys.executable, str(Path(planner.__file__)), str(value), "--lowest", "21", "--highest", "108"], capture_output=True, text=True)
                self.assertEqual(run.returncode, 2); self.assertEqual(run.stdout, ""); self.assertNotIn("Traceback", run.stderr)
            if hasattr(os, "mkfifo"):
                fifo = Path(directory) / "fifo.mid"; os.mkfifo(fifo)
                run = subprocess.run([sys.executable, str(Path(planner.__file__)), str(fifo), "--lowest", "21", "--highest", "108"], capture_output=True, text=True, timeout=3)
                self.assertEqual(run.returncode, 2); self.assertIn("reguläre Datei", run.stderr)


if __name__ == "__main__":
    unittest.main()
