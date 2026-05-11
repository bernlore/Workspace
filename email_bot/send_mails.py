import csv
import smtplib
import time
import os
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_PASS")
SENDER_NAME = "Bernd Lorenzer"
SUBJECT = "Sponsoringanfrage - HTL Weiz Maturaball 2027"

REQUIRED_FILES = [".env", "email_inhalt.md", "Sponsoring_Flyer.pdf", "emails.csv"]


def check_files():
    missing = [f for f in REQUIRED_FILES if not os.path.exists(f)]
    if missing:
        print(f"FEHLER: Folgende Dateien fehlen: {', '.join(missing)}")
        sys.exit(1)


def load_template():
    with open("email_inhalt.md", encoding="utf-8") as f:
        return f.read()


def load_recipients():
    recipients = []
    with open("emails.csv", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            firma = row.get("Firma", "").strip()
            email = row.get("Email", "").strip()
            if firma and email:
                recipients.append({"firma": firma, "email": email})
    return recipients


def build_message(firma, email, body_template):
    body = body_template.replace("{Firma}", firma)

    msg = MIMEMultipart()
    msg["From"] = f"{SENDER_NAME} <{GMAIL_USER}>"
    msg["To"] = email
    msg["Subject"] = SUBJECT
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with open("Sponsoring_Flyer.pdf", "rb") as attachment:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(attachment.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", 'attachment; filename="Sponsoring_Flyer.pdf"')
    msg.attach(part)

    return msg


def send_emails():
    if not GMAIL_USER or not GMAIL_PASS:
        print("FEHLER: GMAIL_USER oder GMAIL_PASS fehlen in der .env Datei.")
        sys.exit(1)

    check_files()

    template = load_template()
    recipients = load_recipients()

    if not recipients:
        print("FEHLER: Keine Empfänger in emails.csv gefunden.")
        sys.exit(1)

    print(f"Starte Versand an {len(recipients)} Empfänger...\n")

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(GMAIL_USER, GMAIL_PASS)
    except smtplib.SMTPAuthenticationError:
        print("FEHLER: Authentifizierung fehlgeschlagen. Bitte GMAIL_USER und GMAIL_PASS prüfen.")
        sys.exit(1)
    except Exception as e:
        print(f"FEHLER: Verbindung zu Gmail fehlgeschlagen: {e}")
        sys.exit(1)

    for i, recipient in enumerate(recipients):
        firma = recipient["firma"]
        email = recipient["email"]

        print(f"Test-Versand an {firma} ({email}) wird vorbereitet...")

        try:
            msg = build_message(firma, email, template)
            server.sendmail(GMAIL_USER, email, msg.as_string())
            print(f"  Erfolgreich gesendet an {firma} ({email})")
        except Exception as e:
            print(f"  FEHLER beim Senden an {firma} ({email}): {e}")

        if i < len(recipients) - 1:
            time.sleep(4)

    server.quit()
    print(f"\nVersand abgeschlossen. {len(recipients)} E-Mail(s) verarbeitet.")


if __name__ == "__main__":
    send_emails()
