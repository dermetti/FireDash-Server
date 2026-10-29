# Phase 3.3 – Magic-Link API und Berichtsportal

Die Implementierung verwendet ReportSubmission, das unveränderte JWE-Protokoll,
ReportDocument v1, den generischen A4-Renderer und die bestehende verifizierte
Department-Mailprovider-Auflösung. PDF-Mail bleibt unverändert verfügbar.
Es gibt keine Berichtstyp-Enums, zusätzliche Berichtsfelder oder allgemeine
Portalbenutzerkonten.

## Tablet-Vertrag

Authentifizierung: bestehender Installation-Bearer, aktuelle Lease und aktive
Tablet-/Fahrzeug-/Stations-/Department-Zuordnung. Die App authentifiziert ihre
Benutzer; dieses Protokoll weist dem Server **nur die Installation** nach.

`POST /api/v1/tablet/report-transfers`, `Content-Type: application/json`:

```json
{
  "transfer_request_id": "00000000-0000-4000-8000-000000000001",
  "submission_ids": ["00000000-0000-4000-8000-000000000002"]
}
```

Genau diese zwei Attribute; maximal 4096 Request-Bytes, 1–20 unterschiedliche
Submission-UUIDs, Reihenfolge ohne Bedeutung. Alle Einträge müssen READY,
unabgelaufen, aus derselben Installation und für denselben gespeicherten
Empfänger sein; Department/Station werden erneut geprüft. Nur ReportDocument-v1-
Pakete sind portalgeeignet. Keine E-Mail oder Personal-ID im Request.

Der Server löst den Empfänger über `resolve_report_recipient()` auf: aktive
Person, Einsatzleiterberechtigung, verifizierte E-Mail und aktuelle Domainpolicy.
Die Freigabe wird zusätzlich über einen HMAC-Fingerprint an das bei Ausstellung
verifizierte Postfach gebunden; eine Änderung der Adresse sperrt den bisherigen
Berichtszugriff. Die Adresse selbst wird nicht im Transfer gespeichert.

Erste Anforderung: HTTP 201; identische Wiederholung: HTTP 200, ohne erneuten
Versand. Beispielantwort:

```json
{"transfer_id":"00000000-0000-4000-8000-000000000003","status":"SENT"}
```

Neutrale Zustände: PROCESSING, SENT, FAILED, UNKNOWN, REVOKED. Keine Link-URL,
Tokens, Adressen oder Berichtsdaten in Antworten. Gleiche Request-ID mit anderer
Berichtsauswahl: 409. Ungültiges JSON/Format: 400, zu groß: 413, unzulässige
Installation/Empfänger/Auswahl: 403 bzw. bestehender Authentifizierungsfehler;
nicht verfügbarer Bericht: 404. Rate-Limit: 429, fehlende HTTPS-Origin: 503.
Ein Replay reserviert die alte Generation und erzeugt auch nach Widerruf keinen
neuen Zugang; dafür ist ausdrücklich eine neue Request-ID nötig.

`POST /api/v1/tablet/report-transfers/{transfer_id}/revoke` benötigt dieselbe
Installationsautorisierung und wirkt ausschließlich auf deren eigenen Transfer.
Wiederholung ist sicher (200 REVOKED), fremde/unbekannte ID: 404.
Berichtsdaten werden dabei weder gelöscht noch länger aufbewahrt.

Standardlimits: drei Anforderungen pro Empfänger/Installation in einer rollenden
Minute und zehn pro Installation in einer Stunde. Damit sind schnelle
Ersatzanforderungen möglich. Konfigurierbar über `REPORT_TRANSFER_MAILS_PER_MINUTE`
(1–10) und `REPORT_TRANSFER_MAILS_PER_HOUR` (1–60). Replays zählen nicht erneut;
Widerrufe sind nicht gedrosselt. Der Linkbesitzer kann keine E-Mail anfordern
und kein Rate-Limit ausschöpfen. Die Limits sind konservative Betriebsdefaults.

## Generationen, Versand und Anmeldung

`ReportTransfer.id` ist die eindeutige Zugangs-Generation. Eine partielle
Unique-Constraint erlaubt nur eine ungesperrte Generation je Empfänger und
Installation. Ausstellung, Redemption und Widerruf verwenden denselben
Installations-Transaktionslock. Eine neue Request-ID sperrt atomar alle vorherigen
Generationen dieses Paars und damit deren Sitzungen. Andere Paare bleiben gültig.

Token: `secrets.token_urlsafe(32)`, 256 Bit Zufall; nur SHA-256 wird gespeichert.
Gültigkeit exakt 60 Minuten ab Ausstellung. Der Klartext lebt ausschließlich im
Ausstellungs-/Mailaufruf im Speicher. Die E-Mail enthält eine neutrale Nachricht
und den HTTPS-Link unter `FIREDASH_PUBLIC_ORIGIN`, ohne Namen, Berichtsdaten oder
Anhänge. Versand erfolgt nach dem Commit über den bereits verifizierten
Department-Provider und mit genau einem `send()`-Aufruf. Ablehnung wird FAILED,
ein ungewisses Providerergebnis UNKNOWN; ein Prozessabbruch kann PROCESSING
stehenlassen. `mail_code` hält einen bereinigten Fehlergrund. Keiner dieser
Fälle wird automatisch erneut versendet. Eine neue explizite Request-ID ist
möglich und ersetzt den alten Zugang. Nur SENT kann zur Anmeldung dienen;
Providerannahme beweist keine Zustellung im Postfach.

| URL | Verhalten |
| --- | --- |
| GET `/reports/access/<token>/` | Bestätigungsseite; keinerlei Anmeldung, Verbrauch, Widerruf oder Versand |
| POST `.../redeem/` | CSRF-geprüft, atomar genau ein Verbrauch, Sessionrotation, Redirect nach `/reports/` |
| POST `.../revoke/` | Beim verbrauchten Token zunächst Bestätigungsansicht, danach ausdrücklicher POST `confirm=yes` |
| GET `/reports/` | Liste ausschließlich der gewählten, aktuell verfügbaren Berichte |
| GET `/reports/<report_id>/` | Generische Detailansicht aller ReportDocument-Blöcke |
| GET `.../pdf/` | Expliziter PDF-Download im Speicher |
| GET `.../photos/<attachment_id>/` | Expliziter Original-JPEG-Download |
| GET `.../photos/<attachment_id>/preview/` | In-Memory-JPEG-Thumbnail, maximal 640 × 480, ohne EXIF |
| POST `/reports/logout/` | Sitzung serverseitig sperren und Django-Session löschen |
| POST `/reports/revoke/` | Bestätigungsansicht, dann `confirm=yes`: nur diese Generation sperren |

Verbrauchte Tokens können ausschließlich ihre eigene Generation sperren, solange
ihre ursprünglichen 60 Minuten noch nicht verstrichen sind. Dafür genügt der
Tokenbesitz, auch wenn Empfängerberechtigung oder Lease inzwischen entfallen
sind. Kein erneutes Anmelden. Unter dem Ausstellungslock wird der Token nochmals
gelesen: Ein alter Token kann eine später ausgestellte Generation niemals
beeinflussen. GET und HEAD führen keine Zustandsänderung durch; Aktionen erfordern
POST und CSRF. Nach Ablauf erscheint die neutrale Aufforderung, einen neuen Link
in der Tablet-App anzufordern. Ein Nuisance-Revoke ist bewusst möglich.

## Portal und Zugriffsschutz

Serverseitige DB-Django-Sessions plus `ReportPortalSession` mit Sessionkey-Hash,
Transferbindung, letztem Zugriff und Logoutzeitpunkt. Bei Anmeldung wird die
bisherige Django-Session verworfen (auch eine vorhandene Administratoridentität),
die ID neu erzeugt und das CSRF-Geheimnis rotiert. Das Portal verleiht keine
Administratorrechte. Inaktivitätslimit: 30 Minuten; aktive Sitzungen dürfen nach
Ablauf der 60 Token-Minuten weiterarbeiten, solange ihre Berichte verfügbar sind.

Jede Liste, Detailansicht, Vorschau und jeder Download prüft Generation,
Sessionbindung, Inaktivität, aktuelle Empfängerberechtigung und Postfachbindung,
historischen Installations-/Departmentscope, Transfermitgliedschaft und effektive
Report-TTL. Seit Phase 3.3.1 hängt ein ausgestellter Zugang weder von der heutigen
Lease noch von der aktuellen Fahrzeug-/Stationszuordnung ab; explizite Sperren
werden weiterhin geprüft (siehe Nachtrag unten).
Die Portal-Lader autorisieren die tatsächliche Transferfreigabe; sie rufen keinen
Tablet-Export mit einer vorgetäuschten Tabletidentität auf. Die geprüfte
Payload-Integritätsroutine wird gemeinsam verwendet. Kenntnis einer UUID gewährt
keinen Zugriff. Abgelaufene Berichte werden in Listen ausgelassen; direkter Zugriff
scheitert. V1-Altbestände ohne ReportDocument werden unverändert behandelt wie in
[Phase 3.2](report-transfer-phase3-2.md), ohne erfundene Datenmigration.
Zur Anmeldung muss mindestens ein ausgewählter Bericht noch verfügbar sein;
der zwischenzeitliche Ablauf eines anderen Berichts blockiert diesen nicht.

Eigenständige deutsche Templates, lokales CSS, keine Adminnavigation,
Drittanbieterressourcen oder Skripte. Django escaped alle Nutztexte; keine
HTML-Interpretation. Header, Text, Fields, Tabellen und Attachment-Verweise werden
generisch dargestellt. Clientangaben zur Finalisierung sind als solche bezeichnet;
Serverannahme wird getrennt angezeigt. Zeitangaben des Servers sind explizit UTC.

Antworten unter `/reports/`: `Cache-Control: private, no-store`, `Referrer-Policy:
no-referrer`, CSP mit ausschließlich lokalen Styles/Bildern und Formularen,
`frame-ancestors 'none'`, `X-Frame-Options: DENY`, nosniff und noindex. Portal-Cookies
werden auch unter lokalen Development-Settings als Secure/SameSite=Lax gesetzt;
Sessioncookies zusätzlich HttpOnly. Daher lokal HTTPS verwenden. Moderne Browser
senden beim Formular-POST einen Origin-Header: Django prüft ihn zusammen mit CSRF.
Fehlender Origin und durch no-referrer fehlender Referer werden bewusst nicht
durch eine CSRF-Ausnahme umgangen. Fremde Origins sind verboten.

## Downloadabschluss, TTL und Cleanup

Keine permanenten PDFs oder Vorschauen, keine öffentlichen Storage-URLs. Originale
JPEGs und PDFs erhalten `Content-Disposition: attachment` mit servergeneriertem
UUID-Dateinamen. HEAD/erkennbare Prefetch-Anfragen zählen nicht. HTML und Thumbnails
erzeugen niemals Downloadereignisse.

WSGI- und ASGI-Streaming-Iteratoren prüfen den Zugriff vor jedem begrenzten Chunk
erneut. Erst bei normaler vollständiger Iteratorbeendigung wird der wirkliche
Abschlusszeitpunkt erfasst und `record_successful_download()` aufgerufen. Ein
frühzeitiges `close()`, Übertragungsfehler oder Widerruf vor einem weiteren Chunk
erreichen diesen Hook nicht. PDF-Erzeugung und Streaming halten keine DB-Locks;
nur Autorisierung, Payload-Lesen und Abschlussverbuchung benutzen kurze
Transaktionen. `X-Accel-Buffering: no` vermeidet die normale Nginx-Pufferung.

Eine kleine atomar veröffentlichte `.receipt`-Datei im privaten Reportspool enthält
nur interne Submission-PK, tatsächlichen Abschlusszeitpunkt und seit Phase 3.3.1
optional Download-/Transfer-UUID sowie Dateityp für das Audit, nie Inhalte oder
Tokens. Nach erfolgreichem DB-Update wird sie entfernt. Bei einem DB-Fehler
erzwingt sie bereits bei Status-/Zugriffsprüfungen die kürzere Frist;
`expire_report_submissions` verarbeitet sie erneut und entfernt sie nach der
Verbuchung. Unterbrochene temporäre `.receipt-tmp`-Dateien werden nach einer Stunde
als Orphans entfernt. Wiederholte Downloads und Wiederholungen der Verbuchung
verlängern niemals die Frist. Spät wiederhergestellte echte Abschlussereignisse
behalten ihren ursprünglichen Zeitpunkt.

Effektive TTL: Minimum aus gespeichertem Ablauf, Annahme + 7 Tage und frühestem
erfolgreichen ausdrücklichen Download + 48 Stunden. Zugriff endet zur Deadline
unabhängig vom Cleanup-Takt. Der vorhandene retryfähige Cleanup entfernt das
Ciphertext-Payload; kleine Identitäts-/Idempotenzdatensätze bleiben bestehen.

Technische Grenze: Normale Iteratorbeendigung belegt die vollständige Übergabe
an den Server-/Proxy-Transport, nicht den Empfang oder das Speichern im Browser.
Bereits ausgelieferte Bytes können bei Widerruf nicht zurückgeholt werden.
Ein Verbindungsabbruch kann serverseitig erst nach Pufferauslieferung erkannt
werden. Ein Prozessabbruch genau zwischen letzter Übergabe und Empfangsnotiz
oder ein gleichzeitiger Ausfall von Spool und Datenbank bleibt ohne atomaren
Nachweis; das ist keine behauptete Browserzustellgarantie. Standardmäßig bleibt
stets die ursprüngliche Siebentagesobergrenze erhalten. Nach einem aufgezeichneten
Abschluss wird die 48-Stunden-Frist auch bei späterer DB-Störung eingehalten.

## Logging und vorbereitete Betriebsintegration

Tokenpfade werden in Django-Request-/CSRF-/Development-Logs redigiert. Unerwartete
Portalfehler liefern eine neutrale 503 und einen bereinigten Ereignistext ohne
Exceptiondetails oder Payload-Locals. Providerfehler werden ausschließlich in
festen Ergebnis-Codes gespeichert, ohne Exceptiontext zu loggen.

Die Nginx-Vorlage unterdrückt Access- und routebezogene Fehlerlogs für
`/reports/access/`; HTTP-Redirects loggen ebenfalls keine URLs. Gunicorn hat
Accesslogs weiterhin aus; auch sein optionales Format enthält keine URL/Referer,
und Workerlogger erhalten einen Tokenfilter. Diese **lokalen Vorlagen müssen vor
echter Nutzung übernommen werden**. Zusätzliche Ingress-/WAF-/APM-/Tracing- und
Mailprovider-Systeme müssen denselben Bearerpfad ausschließen und dürfen keine
Mailkörper, Request-/Responsebodies oder Tokens aufzeichnen. Eine alte laufende
Proxykonfiguration wird durch lokale Dateiänderungen nicht geschützt.

[Deploy-Beispiele](../deploy/examples/report-transfer/) werden vom bisherigen
Deployment nicht automatisch installiert: dedizierte Web-Keyring-Credential,
privater 0700-Spool, Expiry-Oneshot und Fünfminutentimer. Der Cleanup benötigt
keinen privaten Berichtsschlüssel; Web benötigt zum Lesen den dedizierten
Report-Keyring sowie die vorhandenen Mail-Credentials. Spool einschließlich
Receipts aus Backups/Snapshots ausschließen. `REPORT_TRANSFER_ROOT` muss zwischen
Web und Cleanup übereinstimmen; Schlüsselversionen bis zum Ablauf aller zugehörigen
Berichte behalten. Django `clearsessions` zusätzlich regelmäßig einplanen.
Die endgültige Aufbewahrung reiner Transfer-/Session-Metadaten bleibt eine
gesonderte Datenschutz-/Betriebsentscheidung. Keine Units aktiviert, Credentials
provisioniert, Serverbereinigung oder Live-Datenbankänderungen vorgenommen.

## Lokale Prüfung

Alle Mails in den fokussierten Tests sind gemockt; Berichte/JPEGs sind synthetisch,
PostgreSQL verwendet ausschließlich `config.settings.test` und Storage liegt im
disposablen pytest-Verzeichnis. Reproduzierbarer kompletter Ablauf einschließlich
Kompromiss-Widerruf und Neuausstellung:

```powershell
$env:DJANGO_SETTINGS_MODULE='config.settings.test'
.venv/Scripts/python.exe -m pytest -p no:cacheprovider apps/report_transfer/tests/test_portal.py -q
```

Enthalten: alle Blöcke/beliebige Berichtstypen, mehrere Berichte, Auswahl-/Scope-
Fehler, aktuelle Berechtigungen/Domainpolicy, Mailfehler, Idempotenz und parallele
Anforderungen, Scanner-GET, parallele Einmalanmeldung, Generationenwiderruf samt
Sitzungen und kontrolliertem Race zwischen Neuausstellung/altem Widerruf,
60-Minuten-Grenze, 30-Minuten-Inaktivität, Session-/CSRF-Rotation, bestätigter
Widerruf, anonyme/fremde Downloads, Thumbnails, PDF/JPEG, unterbrochene und durch
Widerruf gestoppte Streams, Receipt-Recovery/Cleanup, Escaping, Headers und Logs.

Der abschließende fokussierte Regressionslauf umfasst zusätzlich sämtliche
Report-Submission-/ReportDocument-Tests, Report-Admission, PDF-Mail-Delivery und
Idempotenz, Tablet-API sowie Manifest-Konfiguration:

```powershell
.venv/Scripts/python.exe -m pytest -p no:cacheprovider apps/report_transfer/tests apps/outbound_mail/tests/test_report_admission.py apps/outbound_mail/tests/test_report_delivery.py apps/outbound_mail/tests/test_report_delivery_requests.py apps/tablets/tests/test_api.py apps/publications/tests/test_manifests.py -q --tb=short
```

Ergebnis: **267 Tests bestanden in 70,34 Sekunden**, einschließlich 55 neuer
Portaltests und des ASGI-Streamingtests, ohne Warnungen. Django `check`: keine
Probleme. `makemigrations --check --dry-run`: keine fehlenden Migrationen. Ruff-
Prüfung und Formatcheck aller betroffenen Python-Dateien: erfolgreich (29 Dateien
formatiert geprüft). `git diff --check`: erfolgreich. Disposabler Teststorage nach
dem Lauf entfernt. Keine vollständige Testsuite ausgeführt.

## Nachtrag Phase 3.3.1 – Portalautorisierung und Audit

`authorize()` bleibt für Upload, Tablet-Exporte und neue Linkanforderungen
unverändert streng: aktuelle Lease und gültiger Fahrzeug-/Stationsscope.
`check_transfer()` prüft ausgestellte Portalzugänge separat anhand gespeicherter
Sicherheitszustände. ACTIVE und STALE sind zulässig, auch nach Lease-Ablauf oder
Fahrzeug-/Stationswechsel. REVOKED/REPLACED, gesetztes `revoked_at`, deaktiviertes
Tablet, INACTIVE/LOST/RETIRED und nicht aktives Department sperren weiterhin.
Empfängerberechtigung, verifizierte unveränderte Mailbindung, Domainpolicy,
Generation, Sessionbindung, Einmalanmeldung, Inaktivität und TTL bleiben wirksam.
Der historische Bericht wird nicht umgehängt: Installation, Department, Empfänger,
Station und IDs müssen zusätzlich mit dem authentifizierten JWE-Umschlag
übereinstimmen. Freigaben erschließen ausschließlich ihre ausgewählten Berichte.

Alle folgenden Ereignisse verwenden direkt `apps.audit.services.record_event()`:

| Aktion (`report_transfer.`) | Tatsächlicher Auslöser |
| --- | --- |
| `submission_accepted` | Erstmalige Annahme, atomar mit dem Submission-Datensatz |
| `created` | Neue Transfer-Generation, atomar mit der Freigabe |
| `link_issued` | Einmaliger Versandversuch abgeschlossen; `result` SENT/FAILED/UNKNOWN und bereinigter `code`, keine Zustellbehauptung |
| `login_succeeded` | Tokenverbrauch und Portal-Session atomar gespeichert |
| `login_failed` | Abgewiesener Einlöse-POST; kein Erfolg bei abgelaufenem/verwendetem/gesperrtem Zugang |
| `revoked` | Zustandswechsel mit Quelle `tablet`, `portal`, `consumed_token` oder `replacement` |
| `pdf_generated` | Generischer Renderer erfolgreich, unabhängig vom späteren Downloadabschluss |
| `download_completed` | Vollständige serverseitige PDF-/JPEG-Streamübergabe, auch bei späterer Receipt-Recovery |
| `content_purged` | Verschlüsselten Inhalt entfernt und EXPIRED gespeichert; Dateifehler bleiben wiederholbar |

Tablet-Aktionen tragen die bestehende `actor_installation_uuid`. Portalaktionen
haben keinen persönlich authentifizierten Django-User und keinen vorgetäuschten
Tablet-Akteur. Ziele sind Transfer- oder Submission-UUIDs; zusätzliche Metadaten
beschränken sich auf notwendige IDs, Serverzeitpunkte, Dateityp und feste
Ergebnis-/Quellcodes. Kein Request-Objekt wird an diese Audit-Aufrufe übergeben:
keine URL, IP, User-Agent, Mailadresse, Token/Hash, Sessionkey oder Berichtsinhalte.
Ein `report_finalized`-Ereignis wird nicht erzeugt.

Fehlversuche bekannter Generationen werden unter dem Installationslock höchstens
einmal je Generation erfasst. Nicht zuordenbare Tokenversuche erzeugen global
höchstens einen anonymen `unidentified_token`-Eintrag pro rollender Minute.
Ein nicht wartender PostgreSQL-Transaktionslock sichert diese Grenze auch zwischen
Workern; weitere Versuche erzeugen keine Audit-Zeilen. Es werden keine
Token-Fingerprints oder Clientdaten für diese Begrenzung gespeichert. GET und vor
der View durch CSRF abgewiesene Anfragen erzeugen keine Loginereignisse.

Idempotente Uploads, Linkanforderungen und Widerrufe erzeugen keine erneuten
Zustandsänderungsereignisse. Jeder tatsächlich abgeschlossene neue Download ist
ein eigenes Ereignis; Wiederverarbeitung desselben Abschlussbelegs ist keines.
Die vorhandenen temporären Receipts enthalten dafür optional eine zufällige
Download-UUID, Transfer-UUID und `pdf`/`jpeg`; Audit und Retention werden gemeinsam
unter dem Submission-Lock gespeichert. Alte Receipts ohne diese Metadaten bleiben
für Retention verarbeitbar; unbekannte Dateitypen werden nicht nachträglich
erfunden. Die dokumentierten Grenzen der Erkennung vollständiger Übertragung
gelten weiterhin. Keine neuen Modelle oder Migrationen und keine Änderungen an
JWE, ReportDocument, PDF-Layout, Mailprovider-Auswahl, Publications oder Fake-iPad.

Fokussierte PostgreSQL-Validierung mit isoliertem Dateispeicher und gemockter Mail:

```powershell
$env:DJANGO_SETTINGS_MODULE='config.settings.test'
.venv/Scripts/python.exe -m pytest -p no:cacheprovider apps/report_transfer/tests apps/outbound_mail/tests/test_report_admission.py apps/outbound_mail/tests/test_report_delivery.py apps/outbound_mail/tests/test_report_delivery_requests.py apps/tablets/tests/test_api.py -q --tb=short
```

Zusätzliche Tests prüfen Lease-Ablauf/STALE und Neuzuweisung vor und nach
Einmalanmeldung, unveränderten historischen Scope, alle expliziten Sperrpfade,
Audit-Datensparsamkeit und Ergebniszuordnung, parallele/beschränkt protokollierte
Fehlanmeldungen, Zustandsrollback, unterbrochene Downloads, fehlgeschlagene
Löschung und wiederholte Receipt-Verarbeitung. Die vorhandene Append-only-Sperre
des Audit-Systems wird nicht verändert; Zeitfenster werden per Uhr-Mock geprüft.

Ergebnis Phase 3.3.1: **298 Tests bestanden in 109,69 Sekunden**, einschließlich
36 neuer Autorisierungs-/Audit-Tests, bestehender JWE-/ReportDocument-, Idempotenz-,
Portal-/Generationen-/Retention-, PDF-Mail- und Tablet-API-Tests. Keine Warnungen.
Django `check`: keine Probleme. `makemigrations --check --dry-run`: keine Änderungen.
Ruff und Formatcheck: erfolgreich (26 Python-Dateien). `git diff --check`:
erfolgreich. Disposabler Teststorage nach dem Lauf entfernt. Keine vollständige
Testsuite, Git-Schreibaktionen, echten E-Mails, Deployments, Live-Cleanup-Jobs
oder Live-Datenbankänderungen.
