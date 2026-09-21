"""Public legal pages for YAMSI Lite (Meta app-review compliance).

Static HTML only: no secrets, no environment variables, no database
reads, no personal data. Served unauthenticated by /privacy, /terms,
and /data-deletion (see app.py).
"""

CONTACT_EMAIL = "victorotite6@gmail.com"
DELETION_SUBJECT = "YAMSI Lite Data Deletion Request"

_STYLE = (
    "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
    "line-height:1.6;margin:0 auto;max-width:42rem;padding:1.25rem;"
    "color:#1a1a1a}"
    "h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:1.75rem}"
    "a{color:#0b5fff}"
)


def _page(title, body):
    return "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">" \
        "<meta name=\"viewport\" content=\"width=device-width," \
        "initial-scale=1\"><title>%s</title>" \
        "<style>%s</style></head><body>%s</body></html>" \
        % (title, _STYLE, body)


PRIVACY_HTML = _page("YAMSI Lite Privacy Policy", """
<h1>YAMSI Lite Privacy Policy</h1>
<p>YAMSI Lite helps small businesses record daily operations through
WhatsApp messages sent by their own staff. This policy explains what
information is collected, why, how it is protected, and your rights.</p>
<h2>Information collected</h2>
<ul>
<li>WhatsApp sender number and message content (reports, confirmations,
and review commands sent to the business number).</li>
<li>Business-operation details in those messages, such as sales,
expenses, deposits, and operator piece-work as typed by staff.</li>
<li>Staff identity and business/branch assignment used to route a
message to the correct scope.</li>
<li>Delivery-status events (sent, delivered, read, failed) for
notifications YAMSI Lite sends back to staff.</li>
</ul>
<h2>Why it is processed</h2>
<p>Messages become draft business records for human review; confirmed
records support daily reporting, and delivery events confirm staff
received notifications. Information is never used for advertising or
sold to anyone.</p>
<h2>How it is protected</h2>
<ul>
<li>Stored in a private database with row-level access controls;
application secrets stay server-side and never appear in logs.</li>
<li>Webhook traffic is signature-verified; phone numbers in logs are
masked to the last four digits.</li>
<li>Only registered, owner-authorized business numbers can send or
receive on behalf of a business scope.</li>
</ul>
<h2>Retention</h2>
<p>Draft and confirmed business records are kept while the business
account is active so daily books stay complete. Delivery logs and
temporary processing data are kept only as long as needed for
operations and troubleshooting.</p>
<h2>Sharing</h2>
<p>Messages pass through WhatsApp (Meta) for delivery, subject to
WhatsApp's own terms. YAMSI Lite does not share business records with
any other third party.</p>
<h2>Your rights</h2>
<p>You may request access, correction, or deletion of your information
at any time by contacting the privacy contact below. See
<a href="/data-deletion">/data-deletion</a> for deletion instructions.</p>
<h2>Contact</h2>
<p>Privacy questions: <a href="mailto:%s">%s</a>.</p>
""" % (CONTACT_EMAIL, CONTACT_EMAIL))

TERMS_HTML = _page("YAMSI Lite Terms of Service", """
<h1>YAMSI Lite Terms of Service</h1>
<h2>Acceptable use</h2>
<ul>
<li>Only authorized staff may send business reports to the registered
business number.</li>
<li>Send accurate, lawful messages. Do not submit records for
transactions that did not happen, impersonate another person, or use
the service for spam or harassment.</li>
<li>Review commands (confirm/reject) must come from an authorized
reviewer for that business scope.</li>
</ul>
<h2>Account responsibility</h2>
<p>Each business owner is responsible for keeping their staff list,
business/branch assignments, and registered numbers current, and for
disabling numbers that should no longer send or receive.</p>
<h2>Service availability</h2>
<p>YAMSI Lite depends on WhatsApp, hosting, and database providers and
is offered on a best-effort basis. Maintenance, provider outages, or
messaging limits may delay ingestion or notifications; queued work is
retried within documented bounds.</p>
<h2>Limitations</h2>
<ul>
<li>Raw messages create drafts only. Nothing posts to the business
books without human confirmation, and operator pay stays a preview
until separately confirmed.</li>
<li>YAMSI Lite provides record-keeping, not financial, legal, or tax
advice.</li>
<li>To the extent permitted by law, liability is limited to the service
provided and excludes indirect or consequential loss.</li>
</ul>
<h2>Contact</h2>
<p>Questions about these terms: <a href="mailto:%s">%s</a>.</p>
""" % (CONTACT_EMAIL, CONTACT_EMAIL))

DATA_DELETION_HTML = _page("YAMSI Lite Data Deletion", """
<h1>YAMSI Lite Data Deletion</h1>
<p>To request deletion of your information held by YAMSI Lite:</p>
<ol>
<li>Email <a href="mailto:%s">%s</a>.</li>
<li>Use the subject line:<br><strong>%s</strong></li>
<li>Include your WhatsApp number (the number you messaged from) and
your business name so the correct records can be found.</li>
</ol>
<p>Your identity may be verified before deletion (for example, by
confirming control of the WhatsApp number or through the business
owner) to prevent wrongful deletion of someone else's records.
Deletion is confirmed by reply email once completed.</p>
""" % (CONTACT_EMAIL, CONTACT_EMAIL, DELETION_SUBJECT))
