# MIDI Range Planner

Passt eine MIDI-Stimme auf die verfügbare Klaviatur? Dieses lokale Python-Werkzeug liest die gespeicherten Notennummern, zeigt den benötigten Bereich und findet einheitliche Verschiebungen, bei denen ausgewählten Anschläge innerhalb zweier vorgegebener Grenzen liegen. Es werden keine Noten entfernt, automatisch in andere Oktaven gefaltet oder MIDI-Dateien verändert.

## Start und Beispiel

Benötigt werden **Python 3.10+** und **Mido 1.3.3**. Mido übernimmt die MIDI-Ereignisdekodierung. Im Projektordner eine Umgebung erstellen:

```sh
python3.10 -m venv .venv
```

Danach aktivieren:

```sh
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell stattdessen:
# .venv\Scripts\Activate.ps1
```

```sh
python -m pip install -r requirements.txt
python midi_range_planner.py demo.mid --lowest 36 --highest 72 --channels 1
python -m unittest -v
```

Die Installation benötigt einen Paketdownload; die Auswertung selbst arbeitet offline. JSON erscheint ausschließlich auf stdout. Das Programm öffnet keine MIDI-Ports, sendet nichts an Instrumente, spielt keinen Ton und erzeugt keine Ausgabedatei. Erfolg hat Status **0**, ungültige Eingaben oder Optionen Status **2** mit verständlichem Hinweis auf stderr und ohne Teilbericht.

`demo.mid` enthält eigens erzeugte Testdaten, keine Musikaufnahme. Auf Kanal 1 stehen die Notennummern **48, 72 und 84**. Die Beispielgrenzen 36–72 enthalten einschließlich beider Enden **37 chromatische Tasten**. Der ursprüngliche Ton 84 liegt darüber. Nur die einheitliche Verschiebung **−12 Halbtöne** bringt alle drei Töne hinein:

| Ursprünglich | Verschoben | Anschläge |
| --- | --- | ---: |
| 48 · C3 | 36 · C2 | 1 |
| 72 · C5 | 60 · C4 | 1 |
| 84 · C6 | 72 · C5 | 1 |

Die erwartete Ausgabe steht in `example-report.json`. Weitere Stimmen auf den Kanälen 2 und 10 bleiben durch `--channels 1` ausdrücklich ausgeschlossen. Ohne diese Auswahl werden sie mitgezählt; der gesamte Demonstrationsumfang passt dann nicht durch eine einheitliche Verschiebung auf den Beispielbereich.

## Grenzen und Auswahl richtig setzen

`--lowest` und `--highest` sind **MIDI-Notennummern**, keine Anzahl von Tasten. Prüfe die Endnoten deines Instruments oder der Keyboardzone: Aus „61 Tasten“ allein wird keine bestimmte Lage abgeleitet. Eine lückenlose chromatische Zone wird vorausgesetzt; Splits, fehlende Tasten und individuelle Sample-Zuordnungen werden nicht modelliert.

Standardmäßig zählen alle Spuren und Kanäle. Mit `--channels 1,2` wählst du MIDI-Kanäle **1–16**; `--tracks 1,2` wählt die Spurindizes **ab 0**. Kanal und Spur sind verschiedene Dinge: Derselbe Kanal kann in mehreren Spuren vorkommen. Kanal 10 wird weder automatisch entfernt noch ungeprüft als Schlagzeug bezeichnet. Wähle nur die Stimmen, die du tatsächlich gemeinsam auf diese Zone übertragen möchtest.

Nur **Note On mit Velocity größer null** zählt als Anschlag. Ein Note On mit Velocity 0 bedeutet bei MIDI 1.0 Note Off und erweitert den beobachteten Bereich nicht. Note-Off-Ereignisse, wiederholte Anschläge und Pausen werden nicht als zusätzliche Tonhöhen erfunden. Das Werkzeug bewertet keine Notenlängen oder gleichzeitig erreichbaren Akkorde.

## Den Bericht lesen

Der Bericht enthält Gesamtbereich, getrennte Spur- und Kanalbereiche, Häufigkeiten je Notennummer sowie ursprüngliche Ausreißer. Beispiele nennen Spur, Kanal, Nachrichtenindex und absolute Ticks zum Wiederfinden. Ticks sind keine Takt- oder Sekundenangaben. Gleiche Tickpositionen verschiedener Spuren erhalten nur eine Anzeigeordnung; für den Umfang ist ihre Reihenfolge unerheblich.

Das normale Suchfenster beträgt **−24 bis +24 Halbtöne**. `--shift-min -48 --shift-max 24` erweitert es. Alle passenden Verschiebungen werden aufgeführt; Vielfache von zwölf stehen zusätzlich als oktaverhaltende Auswahl bereit. `smallest_absolute_fit_semitones` ist eine mathematische Auswahl mit kleinstem Betrag, keine musikalische Empfehlung. Eine zu breite Stimme wird von einem zu kleinen Suchfenster unterschieden. Bei fehlenden ausgewählten Anschlägen bleiben Bereich und Verschiebungsplan leer.

Notennamen verwenden hier **60 = C4** und internationale Kreuznamen; **B entspricht deutschem H**. Programme können Oktaven anders beschriften. Maßgeblich bleibt die MIDI-Nummer. Pitch Bend, Stimmung, SysEx, Klangprogramme und geräteeigene Transposition verändern möglicherweise den Klang, werden aber nicht als neue Tastennummern interpretiert. Ein passender Zahlenbereich beweist weder bequeme Handlage noch passende Tonart oder Klang.

## Umfang, Tests und Herkunft

Unterstützt werden SMF Typ 0/1 mit positiver PPQN-Zeitteilung. Typ 2, SMPTE und MIDI-Port-Metadaten werden abgewiesen. Header-, Chunk- und Decodierfehler erhalten klare Meldungen. Dies ist kein vollständiger SMF-Konformitätsvalidator; Midos Toleranz bei Ereigniskodierungen bleibt erhalten. Bewusste Grenzen: **8 MiB**, **256 Spuren**, **64 KiB Rohdaten und 25.000 Nachrichten pro Spur**, insgesamt **200.000 Nachrichten und 100.000 positive Note Ons**. Auch gültige große Exporte können dadurch ausgeschlossen werden. Mido liest jeweils einen begrenzten Track; dessen Nachrichtenlimit wird unmittelbar danach geprüft.

Unabhängig geschriebene MIDI-Bytes und ein erschöpfender Verschiebungsvergleich prüfen unter anderem Filter, inklusive Grenzen, Running Status, Velocity 0 und Schreibfreiheit. Es gibt keine Hardwareprüfung. Code und Anleitung wurden **mit KI-Unterstützung** erstellt; Beispiele sind synthetisch. Neuer Projektcode steht unter MIT, Mido unter seiner eigenen MIT-Lizenz. Das Werkzeug stammt von [PIANOHOF](https://www.pianohof.de/).

Technische Grundlagen: [Mido – Standard MIDI Files](https://mido.readthedocs.io/en/stable/files/midi.html), [MIDI Association – Message List](https://midi.org/expanded-midi-1-0-messages-list), [Microsoft – MIDI-1.0-Note-On mit Velocity 0](https://microsoft.github.io/MIDI/kb/ump-data-format-primer/).
