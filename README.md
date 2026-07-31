# MindMapper V0.9.1

## V0.9.1 – Anschlussseiten und Symbolleiste

Diese Version stabilisiert V0.9.0.

- Nach dem Verschieben wird die Anschlussseite anhand der tatsächlichen
  Lage des Kindes neu bestimmt.
- Ein unten erzeugter und anschließend nach rechts geschobener Knoten
  erhält einen rechten Astabgang.
- Die Inhaltsart lässt sich im Eigenschaftenfenster wieder vollständig
  ändern.
- Typ- und Statussymbol stehen gemeinsam links vor dem Knotentext.
- Ein Klick auf das Statussymbol schaltet weiter:
  **Offen → In Arbeit → Erledigt → Warten → Offen**.
- Rechts im Knoten bleibt nur die Bedienung zum Ein- und Ausklappen.
- Die Änderung des Status ist mit **Strg+Z** rückgängig.


## V0.9.0 – Symbole, Typen und Farben

V0.9.0 setzt bewusst auf ein neues Datenmodell. Eine Kompatibilität zu alten
Testprojekten ist nicht vorgesehen.

### Neues Eigenschaftenfenster

- Knotentyp mit direkt sichtbaren Vorgaben
- frei wählbares Symbol
- Status: Offen, In Arbeit, Erledigt oder Warten
- Hintergrund-, Text- und Rahmenfarbe
- Inhaltsart
- automatisches oder manuelles Layout

### Typvorgaben

Enthalten sind unter anderem:

- Thema
- Aufgabe
- Idee
- Dokument
- Verweis
- Risiko
- Elektronik
- Software
- Mechanik
- Messung
- Bild
- Imkerei

Die Auswahl eines Typs setzt Symbol und Farben gemeinsam. Anschließend lassen
sich Symbol und Farben unabhängig verändern.

### Bedienung

Im Menü **Knoten** sind jetzt sowohl **Ins** als auch **Enter/Tab** sichtbar.
Freies Verschieben, mehrseitige Äste, Unterbaumbewegung und die Übersichtskarte
bleiben erhalten.


## V0.8.1 – Freie Anschlussseiten pro Verbindung

- Jeder Eltern-Kind-Ast wählt seine Seite nach der tatsächlichen Lage des Kindes.
- Ein Elternknoten kann gleichzeitig Verbindungen links, rechts, oben und unten besitzen.
- Beim freien Verschieben wechselt nur der bewegte Ast seine Anschlussseite.
- Geschwister werden je Seite nach ihrer grafischen Position sortiert.
- Die logische Kindreihenfolge im Projektmodell bleibt unverändert.
- Die Anordnung der Imkerei-Mindmap lässt sich damit wieder herstellen.


## V0.8.0 – Projektarbeitsplatz

Die erste 0.8-Version erweitert den reinen Zeichenbereich zu einem kleinen
Projektarbeitsplatz:

- **Eigenschaften-Seitenleiste** für Text, Inhaltsart und Layoutmodus
- **Übersichtskarte** für große Mindmaps
- Verschobene Knoten werden automatisch als **manuell positioniert** markiert
- Manuelle Knoten bleiben beim freien Verschieben geschützt
- Mit „Knoten wieder automatisch anordnen“ kann ein Ast bewusst in das
  automatische Layout zurückgegeben werden
- Der bestehende Schreibfluss, Unterbaum-Transport und Push-and-Shove bleiben erhalten


## V0.7.9 – Freies Verschieben bleibt erhalten

In V0.7.8 wurde der verschobene Knoten nach dem Loslassen durch das
automatische Hierarchie-Layout wieder an seine alte beziehungsweise
berechnete Position zurückgesetzt.

Das ist jetzt korrigiert:

- Der gezogene Knoten bleibt exakt an der vom Benutzer gewählten Position.
- Sein vollständiger Unterbaum bewegt sich weiterhin mit.
- Nach dem Loslassen wird **kein automatisches Neu-Layout** der
  Geschwistergruppe mehr ausgelöst.
- Nur tatsächlich kollidierende fremde Teilbäume dürfen durch
  Push-and-Shove ausweichen.
- Der vom Benutzer bewegte Teilbaum ist für den Algorithmus geschützt.


## V0.7.8 – Platzschaffen auch nach dem Schieben

Beim Verschieben eines Knotens konnte die reine Push-and-Shove-Erkennung
weiterhin chaotische Überlappungen hinterlassen. Nach dem Loslassen wird
deshalb jetzt zusätzlich die betroffene Geschwistergruppe neu angeordnet.

- Der verschobene Unterbaum bleibt zusammen.
- Die Geschwister-Teilbäume werden anhand ihrer vollständigen Höhe verteilt.
- Übergeordnete Ebenen werden anschließend bis zur Wurzel aktualisiert.
- Überlappungen, wie sie nach dem Schieben im Screenshot entstanden sind,
  werden dadurch beseitigt.
- Die gesamte Aktion bleibt mit **Strg+Z** rückgängig.


## V0.7.7 – Teilbäume schaffen beim Wachsen Platz

Wenn ein vorhandener Kindknoten weitere Kinder erhält, wird jetzt nicht nur
dieser einzelne Knoten neu angeordnet. MindMapper aktualisiert anschließend
auch alle übergeordneten Ebenen bis zur Wurzel.

Beispiel:

1. Ein Elternknoten besitzt drei parallele Kinder.
2. Das obere Kind erhält mehrere Unterknoten.
3. Danach erhält das mittlere Kind ebenfalls Unterknoten.

Die Teilbäume werden nun anhand ihrer vollständigen Höhe neu verteilt.
Der mittlere und der untere Ast weichen automatisch aus, sodass sich die
Unterknoten nicht mehr überdecken.

Die Neuberechnung läuft von innen nach außen:

- zuerst der gerade gewachsene Teilbaum,
- dann dessen Geschwistergruppe,
- anschließend die nächsthöhere Ebene,
- bis hinauf zur Wurzel.


## V0.7.6 – Erster Knoten sofort beim Start

Beim Programmstart und nach **Datei → Neu** erscheint automatisch ein
Wurzelknoten in der sichtbaren Mitte.

- Der Knoten befindet sich sofort im Bearbeitungsmodus.
- Der Platzhaltertext ist markiert.
- Man kann direkt schreiben, ohne zunächst einen Knoten anzulegen.
- Der automatisch erzeugte Startknoten wird nicht als eigene Undo-Aktion
  behandelt.


## V0.7.5 – Platzschaffen korrigiert

V0.7.4 startete zwar, die Push-and-Shove-Funktion konnte fremde Äste jedoch
nicht verschieben. Ursache war die Wahl des zu bewegenden Teilbaums: Der
Algorithmus stieg bis zur globalen Wurzel auf und blockierte sich dadurch
selbst.

Die korrigierte Variante:

- verschiebt nur den kollidierenden Schwesterast,
- erkennt Kollisionen an den tatsächlichen Knotenflächen,
- ignoriert leere Flächen innerhalb großer Teilbaum-Begrenzungsrahmen,
- verschiebt benachbarte Teilbäume kaskadierend nach oben oder unten,
- hält einen Sicherheitsabstand von 28 Pixeln ein,
- nimmt den Unterbaum des gezogenen Knotens weiterhin vollständig mit.


## V0.7.4 – Kollisionsvermeidung mit Push-and-Shove

Beim Verschieben eines Knotens verhält sich MindMapper jetzt ähnlich wie ein
Leiterplatteneditor:

- Der komplette Unterbaum bewegt sich mit dem gezogenen Elternknoten.
- Der bewegte Teilbaum behält seine neue Position.
- Kollidierende fremde Teilbäume werden automatisch aus dem Weg geschoben.
- Das Platzschaffen kann sich kaskadierend auf weitere Teilbäume fortsetzen.
- Zwischen den Knoten bleibt ein Sicherheitsabstand erhalten.
- Die gesamte Aktion kann mit **Strg+Z** rückgängig gemacht werden.

Diese Variante ist bewusst ein erster Push-and-Shove-Prototyp. Sie schafft
Platz zwischen Knoten und Unterbäumen; eine spätere Stufe kann zusätzlich
Äste und Verbindungskorridore als Hindernisse berücksichtigen.


## V0.7.3 – Sofort schreiben nach dem Erzeugen

Neu erzeugte Knoten wechseln unmittelbar in die Texteingabe:

- Elternknoten auswählen.
- **Enter** oder **Tab** drücken.
- Der Platzhaltertext des neuen Kindes ist sofort markiert.
- Direkt losschreiben – ein zusätzlicher Doppelklick oder F2 ist nicht nötig.

Auch frei erzeugte Knoten und über das Kontextmenü angelegte Kinder starten
sofort im Bearbeitungsmodus.

Beim Erzeugen eines Kindes bleibt der Elternknoten intern der aktive
Arbeitsknoten. Nach Abschluss der Texteingabe kann deshalb erneut Enter
gedrückt werden, um das nächste Geschwister anzulegen.


## V0.7.2 – Dynamische Austrittspunkte auf jeder Ebene

Die Anschlussregel wird nun rekursiv auf **jeden Elternknoten** angewendet,
nicht nur auf den Wurzelknoten.

- Jeder ausgehende Ast besitzt am Elternknoten einen eigenen Austrittspunkt.
- Die Austrittspunkte werden entsprechend der räumlichen Reihenfolge der
  Kinder harmonisch entlang der passenden Kante verteilt.
- Der Eintritt am Kindknoten bleibt immer mittig auf der zugewandten Seite.
- Fügt man ein Kind hinzu oder verschiebt man einen Knoten, wird die gesamte
  Anschlussgruppe des betroffenen Elternknotens neu berechnet.
- Die Regel gilt ebenso für Kindknoten, Kind-Kind-Knoten und alle weiteren
  Ebenen.

Die Form der Bézier-Kurve wurde dabei nicht grundsätzlich verändert. Die
Verbesserung entsteht durch die richtige Austrittsposition am Elternknoten.


## V0.7.1 – Aufzählungen fortsetzen

Ein neuer Unterknoten übernimmt jetzt die Inhaltsart und Darstellung des
zuletzt vorhandenen Geschwisterknotens derselben Astrichtung.

Damit genügt:

1. Ersten Unterknoten anlegen.
2. Einmal auf **Aufzählung** stellen.
3. Weitere Knoten mit Enter erzeugen.

Die weiteren Geschwister werden automatisch ebenfalls als Aufzählungen
angelegt. Übernommen werden Inhaltsart, Form, Farben sowie Breite und Höhe.
Position, Unterbaum und Ein-/Ausklappzustand werden nicht kopiert.


## V0.7.0 – Objekte, Ansicht und sauberer Astanschluss

Diese Version beginnt die klare Trennung zwischen fachlichem Inhalt und Darstellung.

### Fachliche Inhaltsarten

Jedes Objekt kann unabhängig von seiner Darstellung eine Inhaltsart besitzen:

- Thema
- Abschnitt
- Aufzählung
- Notiz
- Verweis

Die Inhaltsart wird im Objekt gespeichert. Knotenform, Farbe, Größe und Position bleiben Eigenschaften der jeweiligen Ansicht.

### Darstellung

- **Ohne Rahmen** ist wieder verfügbar.
- Aufzählungen werden beim Wechsel der Inhaltsart zunächst rahmenlos dargestellt.
- Rahmenlose Knoten bleiben auswählbar; die Auswahl erscheint nur vorübergehend gestrichelt.
- Notizen erhalten eine dezente rechteckige Darstellung.
- Verweise erhalten eine orangefarbene Vorgabe.
- Die Vorgaben lassen sich anschließend frei überschreiben.

### Astführung

- Rechte Äste beginnen mittig an der rechten Seite des Elternknotens.
- Rechte Äste enden immer mittig an der linken Seite des Kindknotens.
- Linke Äste werden spiegelbildlich angeschlossen.
- Untere Äste beginnen unten mittig und enden oben mittig.
- Alle Geschwister einer Richtung besitzen damit eindeutige, ruhige Anschlusspunkte.


## V0.6.1 – Rückgängig und Wiederholen

- **Strg+Z** macht die letzte Änderung rückgängig.
- **Strg+Y** beziehungsweise die systemübliche Wiederholen-Tastenkombination stellt sie wieder her.
- Menü **Bearbeiten → Rückgängig/Wiederholen**.
- Bis zu 100 Arbeitsschritte werden gespeichert.
- Erfasst werden unter anderem:
  - Knoten anlegen und löschen
  - Kindknoten anlegen
  - Knoten verschieben und umhängen
  - Ast-Richtung ändern und Verbindung lösen
  - Ein-/Ausklappen
  - Umbenennen
  - Größe, Form und Farben ändern
- Beim Öffnen oder Anlegen eines Projekts beginnt eine neue Historie.


## V0.6.0 – Experimentelle Asttypen

Diese Version probiert den neuen Ansatz erstmals praktisch aus:

- **Asttyp 0:** freier Knoten ohne Elternrelation
- **Asttyp 1:** Ast wächst nach rechts
- **Asttyp 2:** Ast wächst nach links
- **Asttyp 3:** Ast wächst nach unten
- Der Asttyp wird jetzt in der Relation gespeichert, nicht nur im Elternknoten.
- Rechte, linke und untere Äste desselben Knotens können gleichzeitig existieren.
- „Ast wächst nach“ bestimmt den Standard für neue Kinder und ordnet vorhandene direkte Äste entsprechend um.
- Beim Umhängen wird der Asttyp aus der räumlichen Position abgeleitet.
- „Freier Knoten (Asttyp 0)“ löst die Elternrelation, ohne den Knoten zu löschen.
- Das Plus/Minus erscheint nur noch bei Knoten mit Kindern und ist direkt anklickbar.
- „Keine Box“ wurde aus dem normalen Menü entfernt; ältere boxlose Knoten erhalten wieder eine dezente Rundbox.


Ein kleiner technischer Mindmap-/Engineering-Graph-Editor auf Basis von Python und PySide6.

## V0.5.2 – Enter bei jedem aktiven Knoten

- Enter und Tab erzeugen jetzt auch dann einen Kindknoten, wenn der aktive Knoten bereits Kinder besitzt.
- Der zuletzt angeklickte Knoten wird unabhängig von kurzfristigen Qt-Fokus- und Auswahlwechseln als aktiver Knoten gespeichert.
- Nach dem Erzeugen bleibt der Elternknoten aktiv.
- Wiederholtes Enter erzeugt beliebig viele Geschwisterknoten.
- Neue Geschwister werden unmittelbar gemeinsam neu angeordnet.

## V0.5.1 – Visuelle Harmonie und Routing

- Verbindungslinien beginnen und enden dynamisch an der tatsächlichen Knotenkontur.
- Linien wachsen abhängig von der Lage nach links, rechts oder unten aus dem Knoten.
- Linke und rechte Kindergruppen werden unabhängig vertikal um den Elternknoten zentriert.
- Nach unten angeordnete Kinder werden mit harmonischem Abstand horizontal zentriert.
- Die Breite und Höhe kompletter Teilbäume fließt in die Anordnung ein.
- Beim Wechsel der Anordnungsrichtung werden vorhandene Unterknoten neu angeordnet.
- Beim Ausklappen wird der wieder sichtbare Teilbaum neu geordnet.
- Nach dem Anlegen und Umhängen werden Geschwister automatisch mit mehr Abstand verteilt.
- Umhängen funktioniert wieder durch Ziehen eines einzelnen Knotens auf einen anderen.
- Bei Mehrfachauswahl bleibt Umhängen weiterhin gesperrt.
- Die Trefferzone zum Umhängen ist bewusst klein, damit normales Verschieben nicht versehentlich die Struktur ändert.

## Erweiterung V0.5.0

- Neue Knoten überdecken keine vorhandenen Knoten mehr:
  kollidierende fremde Teilbäume werden vor dem Anlegen verschoben.
- Mehrfachauswahl wird deutlich blau hervorgehoben.
- Mehrere markierte Knoten können ohne versehentliches Umhängen verschoben werden.
- Umhängen ist jetzt eine bewusste Aktion: `Strg` halten und einen einzelnen Knoten ziehen.
- „Verbindung zum Elternknoten lösen“ erzeugt wieder einen freien Wurzelknoten.
- Knotengröße kann über das Kontextmenü geändert werden.
- Alle V0.4-Formatierungen bleiben erhalten.

## Erweiterung V0.4.0

- Anordnung der Unterknoten ist jetzt für jeden Knoten einzeln auswählbar:
  - nach rechts
  - nach links
  - nach unten
- Neue Unterknoten werden entsprechend der am Elternknoten gewählten Richtung angelegt.
- Knotenform ist pro Knoten auswählbar:
  - keine Box
  - Rechteck
  - runde Ecken
- Farben sind pro Knoten einstellbar:
  - Füllfarbe
  - Rahmenfarbe
  - Textfarbe
- Alle Einstellungen befinden sich im Kontextmenü des Knotens.
- Formatierung wird in der Map-Ansicht gespeichert und bleibt nach Speichern/Laden erhalten.

## Erweiterung V0.3.2

- nach dem Erzeugen eines Unterknotens bleibt der Elternknoten ausgewählt
- wiederholtes `Enter` oder `Tab` erzeugt mehrere Kinder desselben Knotens
- die Auswahl springt nicht mehr automatisch auf den neu erzeugten Unterknoten
- gilt auch für **Kindknoten anlegen** im Kontextmenü

## Erweiterung V0.3.1

- Knotentext per Doppelklick bearbeiten
- `F2` startet die Bearbeitung des ausgewählten Knotens
- `Enter` übernimmt den eingegebenen Text
- `Esc` verwirft die aktuelle Texteingabe
- Kontextmenü enthält **Umbenennen**
- `Enter` und `Tab` erzeugen außerhalb der Texteingabe weiterhin Unterknoten
- vollständige Neuzeichnung der Grafikansicht beseitigt die sichtbaren Rechteck- und Nachziehartefakte beim Verschieben

## Erweiterung V0.3.0

- neue Unterknoten werden auf dem nächsten freien Geschwisterplatz angelegt
- Geschwister liegen nicht mehr direkt übereinander
- Live-Vorschau beim Umhängen während die Maustaste gedrückt bleibt
- möglicher neuer Elternknoten wird mit blauem Rahmen hervorgehoben
- neue Vorschau-Verbindung folgt der Mausbewegung
- endgültiges Umhängen erfolgt erst beim Loslassen
- Baumrelationen besitzen nun eine Reihenfolge (`order`) als Grundlage für spätere Auto-Layouts

## Erweiterung V0.2.6

- Knoten können mit der Maus an einen anderen Knoten umgehängt werden
- beim Loslassen nahe am Zielknoten wird die alte Baumverbindung entfernt
- anschließend wird eine neue Eltern-Kind-Verbindung angelegt
- Zyklen werden verhindert: Ein Knoten kann nicht unter einen eigenen Nachfolger gehängt werden
- **Neu**, **Öffnen**, **Speichern** und **Speichern unter** wurden aus der alten Werkzeugleiste entfernt und bleiben im Menü **Datei**

## Erweiterung V0.2.5

- `Enter` erzeugt einen Unterknoten zum ausgewählten Knoten
- `Tab` bleibt ebenfalls für Unterknoten verfügbar
- neues Menü **Datei** mit **Neu**, **Öffnen**, **Speichern** und **Speichern unter**
- neues Menü **Knoten** mit **Neuer Knoten** und **Unterknoten**

## Erweiterung V0.2.4

- sichtbare Schaltfläche **Unterknoten**
- Taste `Tab` erzeugt einen Unterknoten zum ausgewählten Knoten
- Unterknoten wird automatisch verbunden und ausgewählt
- Hinweis, wenn kein Elternknoten ausgewählt ist

## Fehlerkorrektur V0.2.3

- Initialisierungsfehler beim Erzeugen des ersten Knotens behoben
- Beziehungsliste wird jetzt vor `setPos()` angelegt
- `itemChange()` zusätzlich gegen frühe Qt-Aufrufe abgesichert

## Fehlerkorrektur V0.2.2

- Absturz beim Doppelklick auf die freie Fläche behoben
- korrekte Verwendung der QGraphicsView-Transformation

## Fehlerkorrektur V0.2.1

- sichtbarer Button **Neuer Knoten**
- Taste `Einfg` erzeugt einen Knoten in der Bildschirmmitte
- Rechtsklick auf freie Fläche erzeugt einen Knoten
- Doppelklick auf freie Fläche korrigiert

## Neu in V0.2

- Dateikopf mit getrennter Programm- und Dateiformat-Version
- Internes Modell aus:
  - Objekten
  - Beziehungen
  - Maps/Ansichten
- Position, Größe und `collapsed` gehören zur jeweiligen Map
- Mehrere Maps können dieselben Objekte zeigen
- Grundlage für spätere Untermaps
- Automatische Migration einfacher V0.1-Dateien
- Sicheres Speichern über temporäre Datei
- VS-Code-Konfiguration
- PyInstaller-Konfiguration

## Start

```bash
python -m venv .venv
```

Windows:

```powershell
.venv\Scripts\activate
pip install -r requirements.txt
python -m mindmapper
```

Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
python -m mindmapper
```

## Bedienung

- Doppelklick auf freie Fläche: neuer Knoten
- Knoten ziehen: Position ändern
- Rechtsklick auf Knoten:
  - Kindknoten anlegen
  - Ein-/Ausklappen
  - Knoten löschen
- `Strg+S`: speichern
- `Strg+O`: öffnen
- `Strg+N`: neues Projekt
- Mausrad: Zoom
- Mittlere Maustaste: Ansicht verschieben

## Dateiformat

```json
{
  "format": {
    "name": "MindMapper Project",
    "version": "0.2.0"
  },
  "generator": {
    "name": "MindMapper",
    "version": "0.2.0"
  },
  "project": {},
  "objects": {},
  "relations": {},
  "maps": {}
}
```

Das Mindmap-Bild selbst wird nicht als starres Bild gespeichert. Gespeichert werden Objekte,
Beziehungen und die jeweilige Map-Darstellung.

## EXE erzeugen

```bash
pip install pyinstaller
pyinstaller MindMapper.spec
```


## Neu in 0.9.2

- Status ist optional und wird als Symbol neben dem Knotentyp angezeigt.
- Typ-, Statussymbol und Text werden vertikal auf einer gemeinsamen Mittellinie ausgerichtet.
- Neue Knotenform **Unterstrichen**; ihre Äste können nur nach links oder rechts wachsen.
- Alte Projekte mit Status `open` werden weiterhin geladen und ohne Statussymbol dargestellt.
