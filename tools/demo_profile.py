"""Build a Thunderbird profile with an invented mailbox, for tests and screenshots.

    python tools/demo_profile.py OUT_DIR [--lang de|en]

OUT_DIR becomes a complete profile: prefs.js with only "Local Folders" and
mbox files under Mail/Local Folders/. All people, companies and addresses are
made up (example.* domains). Dates are relative to today, so "latest" questions
work whenever the profile is built. Start Thunderbird with it:

    thunderbird --profile OUT_DIR --no-remote
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import uuid
from email.message import EmailMessage
from pathlib import Path

# (folder, days ago, sender name, sender address, subject, body)
MAILS = {
    "en": [
        ("Inbox", 2, "Northwind Insurance", "service@northwind-insurance.example",
         "Your car insurance renews on 1 December",
         "Dear Alex Miller,\n\nyour car insurance (policy KF-20931) renews automatically on "
         "1 December. The new yearly premium is 412.80 EUR.\n\nIf you want to cancel, we need "
         "your notice by 30 November.\n\nYour documents: https://northwind-insurance.example/"
         "documents/KF-20931-renewal.pdf\n\nKind regards\nNorthwind Insurance"),
        ("Inbox", 3, "Dr. Petra Lang", "praxis@lang-dental.example",
         "Reminder: dental check-up on Thursday",
         "Hello Alex,\n\nthis is a reminder of your check-up on Thursday at 8:30 am.\n"
         "Please bring your insurance card.\n\nDental practice Dr. Lang\nGarden Street 4"),
        ("Inbox", 4, "Ben Carter", "ben.carter@example.org", "Slides for Friday",
         "Hi Alex,\n\nhere are my slides for the project meeting on Friday. Could you add the "
         "budget figures on slide 7?\n\nThe meeting is at 10:00 in room 2.14.\n\nCheers\nBen"),
        ("Inbox", 6, "Riverside Primary School", "office@riverside-school.example",
         "Parents' letter: school trip on 14 November",
         "Dear parents,\n\nclass 3b goes to the science museum on 14 November. Please return "
         "the signed consent form by 7 November and give your child 12 EUR for the entrance "
         "fee.\n\nForm: https://riverside-school.example/forms/trip-3b.pdf\n\n"
         "Mrs. Green, class teacher"),
        ("Inbox", 8, "Brightwave Energy", "billing@brightwave.example",
         "Your electricity bill for October",
         "Dear customer,\n\nyour bill for October is ready: 86.40 EUR, debited on the 15th.\n"
         "Consumption: 231 kWh.\n\nView bill: https://brightwave.example/account/bills/2026-10\n"
         "\nBrightwave Energy"),
        ("Inbox", 9, "Parcel Express", "noreply@parcel-express.example",
         "Your parcel has been delivered",
         "Your parcel 7781-2290 was delivered today at 14:12 and left with your neighbour "
         "(No. 12).\n\nParcel Express"),
        ("Inbox", 12, "Sophie Wagner", "sophie.wagner@example.net", "Dinner on Saturday?",
         "Hey Alex,\n\nwe're having a few friends over on Saturday at 7. Would you like to come? "
         "Bring Sam if you like!\n\nSophie"),
        ("Inbox", 15, "Hill & Partner Tax Advisors", "office@hill-tax.example",
         "Documents for your 2025 tax return",
         "Dear Mr. Miller,\n\nplease send us the following documents for your 2025 tax "
         "return by 31 January:\n- annual salary statement\n- receipts for donations\n"
         "- invoices for craftsmen's services\n\nUpload portal: "
         "https://hill-tax.example/upload/miller\n\nKind regards\nJulia Hill"),
        ("Inbox", 18, "City Sports Club", "treasurer@citysports.example",
         "Membership fee 2027",
         "Dear members,\n\nthe membership fee for 2027 stays at 96 EUR per year and will be "
         "collected in January. The general meeting is on 20 November at 19:30 in the club "
         "house.\n\nThe board"),
        ("Inbox", 21, "Skyline Internet", "invoice@skyline-net.example",
         "Invoice 2026-10 for your internet contract",
         "Dear customer,\n\nyour invoice for October: 39.99 EUR.\nContract: Fiber 250\n\n"
         "Invoice PDF: https://skyline-net.example/invoices/2026-10.pdf\n\nSkyline Internet"),
        ("Archive", 40, "Hill & Partner Tax Advisors", "office@hill-tax.example",
         "Your 2024 tax assessment",
         "Dear Mr. Miller,\n\nthe tax office has issued your assessment for 2024. You will get "
         "a refund of 642 EUR. We checked the notice, it is correct.\n\nKind regards\n"
         "Julia Hill"),
        ("Archive", 55, "Brightwave Energy", "billing@brightwave.example",
         "Your electricity bill for September",
         "Dear customer,\n\nyour bill for September: 79.10 EUR. Consumption: 212 kWh.\n\n"
         "Brightwave Energy"),
        ("Archive", 70, "Northwind Insurance", "service@northwind-insurance.example",
         "Confirmation of your address change",
         "Dear Alex Miller,\n\nwe have updated your address to Maple Road 8, 12345 Sample "
         "Town.\n\nNorthwind Insurance"),
        ("Archive", 90, "Ben Carter", "ben.carter@example.org", "Holiday photos",
         "Hi Alex,\n\nhere's the link to the photos from the lake: "
         "https://photos.example.org/album/lake-2026\n\nBen"),
    ],
    "de": [
        ("Inbox", 2, "Nordwind Versicherung", "service@nordwind-versicherung.example",
         "Ihre Kfz-Versicherung verlängert sich zum 1. Dezember",
         "Sehr geehrte Frau Müller,\n\nIhre Kfz-Versicherung (Vertrag KF-20931) verlängert sich "
         "automatisch zum 1. Dezember. Der neue Jahresbeitrag beträgt 412,80 EUR.\n\n"
         "Eine Kündigung muss bis zum 30. November bei uns eingehen.\n\nIhre Unterlagen: "
         "https://nordwind-versicherung.example/dokumente/KF-20931-verlaengerung.pdf\n\n"
         "Mit freundlichen Grüßen\nNordwind Versicherung"),
        ("Inbox", 3, "Zahnarztpraxis Dr. Lang", "praxis@zahnarzt-lang.example",
         "Erinnerung: Kontrolltermin am Donnerstag",
         "Hallo Frau Müller,\n\nwir erinnern an Ihren Kontrolltermin am Donnerstag um 8:30 Uhr. "
         "Bitte bringen Sie Ihre Versichertenkarte mit.\n\nPraxis Dr. Lang\nGartenstraße 4"),
        ("Inbox", 4, "Ben Becker", "ben.becker@example.org", "Folien für Freitag",
         "Hallo Anna,\n\nanbei meine Folien für das Projekttreffen am Freitag. Kannst du auf "
         "Folie 7 noch die Budgetzahlen ergänzen?\n\nWir treffen uns um 10 Uhr in Raum 2.14.\n\n"
         "Gruß\nBen"),
        ("Inbox", 6, "Grundschule am Fluss", "sekretariat@grundschule-am-fluss.example",
         "Elternbrief: Ausflug am 14. November",
         "Liebe Eltern,\n\ndie Klasse 3b fährt am 14. November ins Naturkundemuseum. Bitte geben "
         "Sie die unterschriebene Einverständniserklärung bis zum 7. November ab und geben Sie "
         "Ihrem Kind 12 EUR Eintritt mit.\n\nFormular: https://grundschule-am-fluss.example/"
         "formulare/ausflug-3b.pdf\n\nFrau Grün, Klassenlehrerin"),
        ("Inbox", 8, "Hellwelle Energie", "rechnung@hellwelle.example",
         "Ihre Stromrechnung für Oktober",
         "Guten Tag,\n\nIhre Rechnung für Oktober liegt bereit: 86,40 EUR, Abbuchung am 15.\n"
         "Verbrauch: 231 kWh.\n\nRechnung ansehen: https://hellwelle.example/konto/rechnungen/"
         "2026-10\n\nHellwelle Energie"),
        ("Inbox", 9, "Paket Express", "noreply@paket-express.example",
         "Ihr Paket wurde zugestellt",
         "Ihr Paket 7781-2290 wurde heute um 14:12 Uhr zugestellt und bei Ihrem Nachbarn "
         "(Nr. 12) abgegeben.\n\nPaket Express"),
        ("Inbox", 12, "Sophie Wagner", "sophie.wagner@example.net", "Essen am Samstag?",
         "Hey Anna,\n\nwir haben am Samstag um 19 Uhr ein paar Freunde da. Hast du Lust? "
         "Bring gern Sam mit!\n\nSophie"),
        ("Inbox", 15, "Steuerberatung Hügel & Partner", "kanzlei@huegel-steuer.example",
         "Unterlagen für Ihre Steuererklärung 2025",
         "Sehr geehrte Frau Müller,\n\nbitte senden Sie uns bis zum 31. Januar folgende "
         "Unterlagen für Ihre Steuererklärung 2025:\n- Lohnsteuerbescheinigung\n"
         "- Spendenquittungen\n- Handwerkerrechnungen\n\nUpload-Portal: "
         "https://huegel-steuer.example/upload/mueller\n\nMit freundlichen Grüßen\nJulia Hügel"),
        ("Inbox", 18, "Sportverein Stadtmitte", "kasse@sv-stadtmitte.example",
         "Mitgliedsbeitrag 2027",
         "Liebe Mitglieder,\n\nder Beitrag für 2027 bleibt bei 96 EUR im Jahr und wird im Januar "
         "eingezogen. Die Mitgliederversammlung ist am 20. November um 19:30 Uhr im Vereinsheim."
         "\n\nDer Vorstand"),
        ("Inbox", 21, "Himmelsnetz Internet", "rechnung@himmelsnetz.example",
         "Rechnung 2026-10 für Ihren Internetvertrag",
         "Guten Tag,\n\nIhre Rechnung für Oktober: 39,99 EUR.\nTarif: Glasfaser 250\n\n"
         "Rechnung als PDF: https://himmelsnetz.example/rechnungen/2026-10.pdf\n\n"
         "Himmelsnetz Internet"),
        ("Archive", 40, "Steuerberatung Hügel & Partner", "kanzlei@huegel-steuer.example",
         "Ihr Steuerbescheid 2024",
         "Sehr geehrte Frau Müller,\n\ndas Finanzamt hat Ihren Bescheid für 2024 erlassen. "
         "Sie erhalten 642 EUR zurück. Wir haben den Bescheid geprüft, er ist korrekt.\n\n"
         "Mit freundlichen Grüßen\nJulia Hügel"),
        ("Archive", 55, "Hellwelle Energie", "rechnung@hellwelle.example",
         "Ihre Stromrechnung für September",
         "Guten Tag,\n\nIhre Rechnung für September: 79,10 EUR. Verbrauch: 212 kWh.\n\n"
         "Hellwelle Energie"),
        ("Archive", 70, "Nordwind Versicherung", "service@nordwind-versicherung.example",
         "Bestätigung Ihrer Adressänderung",
         "Sehr geehrte Frau Müller,\n\nwir haben Ihre Adresse geändert auf Ahornweg 8, "
         "12345 Musterstadt.\n\nNordwind Versicherung"),
        ("Archive", 90, "Ben Becker", "ben.becker@example.org", "Urlaubsfotos",
         "Hallo Anna,\n\nhier der Link zu den Fotos vom See: "
         "https://fotos.example.org/album/see-2026\n\nBen"),
    ],
}
OWNER = {"en": ("Alex Miller", "alex.miller@example.com"),
         "de": ("Anna Müller", "anna.mueller@example.com")}
FOLDER_NAMES = {"en": {"Inbox": "Inbox", "Archive": "Archives"},
                "de": {"Inbox": "Inbox", "Archive": "Archives"}}

PREFS = """// Demo profile written by mailchat/tools/demo_profile.py
user_pref("mail.account.account1.server", "server1");
user_pref("mail.accountmanager.accounts", "account1");
user_pref("mail.accountmanager.localfoldersserver", "server1");
user_pref("mail.server.server1.directory-rel", "[ProfD]Mail/Local Folders");
user_pref("mail.server.server1.hostname", "Local Folders");
user_pref("mail.server.server1.name", "{local_name}");
user_pref("mail.server.server1.type", "none");
user_pref("mail.server.server1.userName", "nobody");
user_pref("mail.provider.suppress_dialog_on_startup", true);
user_pref("mail.shell.checkDefaultClient", false);
user_pref("mailnews.start_page.enabled", false);
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("app.donation.eoy.version.viewed", 999);
user_pref("mail.spotlight.firstRunDone", true);
user_pref("mail.winsearch.firstRunDone", true);
user_pref("extensions.autoDisableScopes", 0);
"""


def build(out: Path, lang: str) -> int:
    owner_name, owner_addr = OWNER[lang]
    root = out / "Mail" / "Local Folders"
    root.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now().astimezone().replace(second=0, microsecond=0)
    boxes: dict[str, list[bytes]] = {}
    for i, (folder, days, name, addr, subject, body) in enumerate(MAILS[lang]):
        when = (now - dt.timedelta(days=days)).replace(hour=8 + i % 10, minute=(i * 7) % 60)
        msg = EmailMessage()
        msg["From"] = email.utils.formataddr((name, addr))
        msg["To"] = email.utils.formataddr((owner_name, owner_addr))
        msg["Subject"] = subject
        msg["Date"] = email.utils.format_datetime(when)
        msg["Message-ID"] = f"<{uuid.uuid5(uuid.NAMESPACE_URL, lang + subject)}@demo.example>"
        msg.set_content(body)
        raw = msg.as_bytes(policy=msg.policy.clone(linesep="\n"))
        # mboxrd: escape body lines that start with "From ".
        raw = b"\n".join(b">" + l if l.startswith(b"From ") else l for l in raw.split(b"\n"))
        envelope = f"From - {when.strftime('%a %b %d %H:%M:%S %Y')}\n".encode()
        boxes.setdefault(FOLDER_NAMES[lang][folder], []).append(envelope + raw.rstrip(b"\n") + b"\n\n")
    for folder, mails in boxes.items():
        (root / folder).write_bytes(b"".join(mails))
    for extra in ("Trash", "Unsent Messages"):
        (root / extra).touch()
    local_name = "Lokale Ordner" if lang == "de" else "Local Folders"
    (out / "prefs.js").write_text(PREFS.format(local_name=local_name), encoding="utf-8")
    return len(MAILS[lang])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("--lang", choices=("de", "en"), default="en")
    a = ap.parse_args()
    print(f"{build(a.out, a.lang)} mails written to {a.out}")
