# Privacy policy – Mail Chat (local)

*Deutsche Fassung unten.*

The Mail Chat add-on does not collect, store or transmit any data to the author or to third
parties.

- The add-on talks only to the mailchat server on the same computer (`http://127.0.0.1:8095`).
  It sends your chat questions there and receives answers and references to mails.
- The add-on reads message headers (Message-ID) only to open a mail you click on, and reads
  an attachment only when you ask to send it on. The permission *read your email messages*
  exists for that purpose.
- Files you add to the chat are sent to the mailchat server on the same computer, which
  extracts their text and keeps it in memory only.
- The permission *compose messages* is used only to open a draft in the compose window when
  you ask for it. The add-on never sends a mail itself.
- To open an event dialog, the add-on passes the event (title, time, place, description) to
  the add-on ThunderAI Sparks inside Thunderbird, if installed.
- The mailchat server reads Thunderbird's mail files read-only and passes mail content to the
  language model configured by you. By default this is Ollama on the same computer, so no
  data leaves your machine. If you configure a remote language model, mail content is sent
  to it; that is your choice and outside this add-on.
- There is no telemetry, no analytics, no advertising and no remote code.

Questions: <https://github.com/huppiflupp/mailchat/issues>

---

# Datenschutzerklärung – Mail-Chat (lokal)

Das Add-on Mail-Chat erhebt, speichert oder übermittelt keine Daten an den Autor oder an Dritte.

- Das Add-on spricht nur mit dem mailchat-Server auf demselben Rechner
  (`http://127.0.0.1:8095`). Dorthin schickt es deine Chat-Fragen und bekommt Antworten und
  Verweise auf Mails zurück.
- Das Add-on liest Kopfzeilen von Nachrichten (Message-ID) nur, um eine angeklickte Mail zu
  öffnen, und einen Anhang nur, wenn du ihn weiterschicken lassen willst. Dafür gibt es die
  Berechtigung *E-Mails lesen*.
- Dateien, die du in den Chat gibst, gehen an den mailchat-Server auf demselben Rechner. Er
  liest ihren Text aus und hält ihn nur im Arbeitsspeicher.
- Die Berechtigung *Nachrichten verfassen* dient nur dazu, auf deinen Wunsch einen Entwurf im
  Verfassen-Fenster zu öffnen. Das Add-on verschickt selbst nie eine Mail.
- Für den Termin-Dialog gibt das Add-on den Termin (Titel, Zeit, Ort, Beschreibung) an das
  Add-on ThunderAI Sparks in Thunderbird weiter, falls es installiert ist.
- Der mailchat-Server liest die Mail-Dateien von Thunderbird nur lesend und gibt Mailinhalte
  an das von dir eingestellte Sprachmodell. Standardmäßig ist das Ollama auf demselben
  Rechner, es verlassen also keine Daten den Computer. Stellst du ein entferntes Sprachmodell
  ein, gehen Mailinhalte dorthin; das ist deine Entscheidung und liegt außerhalb dieses Add-ons.
- Es gibt keine Telemetrie, keine Statistik, keine Werbung und keinen nachgeladenen Code.

Fragen: <https://github.com/huppiflupp/mailchat/issues>
