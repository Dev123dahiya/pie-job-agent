"""A local, fictional job-application website for browser tests. Nothing here is a real
employer; it only runs on 127.0.0.1 during tests.

Every case is a page under /jobs/<case>. Form posts go to /submit/<case> and are recorded in
``ApplySite.submissions`` so tests can prove that a dry run never submits anything.
"""

import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

BASE_FIELDS = """
  <div class="field"><label for="first_name">First Name *</label>
    <input id="first_name" name="first_name" autocomplete="given-name" required></div>
  <div class="field"><label for="last_name">Last Name *</label>
    <input id="last_name" name="last_name" autocomplete="family-name" required></div>
  <div class="field"><label for="email">Email *</label>
    <input id="email" name="email" type="email" required></div>
  <div class="field"><label for="phone">Phone *</label>
    <input id="phone" name="phone" type="tel" required {phone_attrs}></div>
  <div class="field"><label for="linkedin">LinkedIn Profile</label>
    <input id="linkedin" name="urls[LinkedIn]"></div>
  <div class="field"><label for="website">Website</label>
    <input id="website" name="urls[Portfolio]"></div>
  <div class="field"><label for="resume">Resume/CV *</label>
    <input id="resume" name="resume" type="file" accept=".pdf" required>
    <div class="error" id="resume-error"></div></div>
  <div class="field"><label for="cover">Cover Letter</label>
    <input id="cover" name="cover_letter" type="file"></div>
{auth_field}
  <fieldset class="field"><legend>Are you willing to relocate? *</legend>
    <label><input type="radio" name="relocate" value="yes" required> Yes</label>
    <label><input type="radio" name="relocate" value="no"> No</label></fieldset>
{notice_field}
  <div class="field"><label for="salary">Expected salary (CTC) *</label>
    <input id="salary" name="salary" required></div>
  <div class="field"><label for="hear">How did you hear about us?</label>
    <input id="hear" name="hear"></div>
  <div class="field"><label for="gender">Gender</label>
    <select id="gender" name="gender"><option value="">Decline</option><option>Female</option>
    <option>Male</option></select></div>
"""

NATIVE_AUTH = """  <div class="field"><label for="auth">Are you legally authorized to work in India? *</label>
    <select id="auth" name="work_auth" required>
      <option value="">Select...</option><option value="1"{yes_selected}>Yes</option><option value="0">No</option>
    </select></div>"""
TEXT_NOTICE = """  <div class="field"><label for="notice">Notice period *</label>
    <input id="notice" name="notice" required></div>"""


def act_select(
    field_id: str, label: str, options: list[str], *, searchable: bool = False, selected: str = ""
) -> str:
    """A react-select style custom dropdown: role=combobox input, aria-expanded/-controls, a
    listbox rendered only while open, the chosen value shown as .select__single-value."""
    import json

    readonly = "" if searchable else "readonly"
    return f"""  <div class="field"><label for="{field_id}-input">{label} *</label>
    <div class="select-shell" data-options='{json.dumps(options)}'
         data-searchable="{str(searchable).lower()}" data-selected="{selected}">
      <div class="select__control">
        <div class="select__value-container"></div>
        <input id="{field_id}-input" role="combobox" aria-expanded="false"
          aria-controls="{field_id}-listbox" aria-autocomplete="list" aria-required="true"
          autocomplete="off" {readonly}>
      </div>
      <div id="{field_id}-listbox" role="listbox" hidden></div>
      <input type="hidden" name="{field_id}">
    </div></div>"""


ACT_SELECT_JS = """<script>
document.querySelectorAll('.select-shell').forEach((root) => {
  const opts = JSON.parse(root.dataset.options);
  const searchable = root.dataset.searchable === 'true';
  const input = root.querySelector('input[role=combobox]');
  const box = root.querySelector('[role=listbox]');
  const vc = root.querySelector('.select__value-container');
  const hidden = root.querySelector('input[type=hidden]');
  const setValue = (v) => {
    vc.innerHTML = v ? '<div class="select__single-value">' + v + '</div>'
                     : '<div class="select__placeholder">Select...</div>';
    hidden.value = v;
  };
  const close = () => { box.hidden = true; input.setAttribute('aria-expanded', 'false');
                        if (searchable) input.value = ''; };
  const render = (q) => {
    const shown = searchable
      ? (q ? opts.filter((o) => o.toLowerCase().includes(q.toLowerCase())) : [])
      : opts;
    box.innerHTML = shown.map((o) => '<div role="option">' + o + '</div>').join('');
    box.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    box.querySelectorAll('[role=option]').forEach((el) => el.addEventListener('mousedown',
      (e) => { e.preventDefault(); setValue(el.textContent); close(); }));
  };
  input.addEventListener('click', () => render(input.value));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') render(input.value);
    if (e.key === 'Escape') close();
  });
  input.addEventListener('input', () => render(input.value));
  setValue(root.dataset.selected);
});
</script>"""

UPLOAD_JS = {
    # The page sends the file as soon as it is chosen (many ATSs do): a dry run must block it.
    "async_upload": """<script>
document.getElementById('resume').addEventListener('change', (e) => {
  const body = new FormData(); body.append('file', e.target.files[0]);
  fetch('/upload', { method: 'POST', body }).catch(() => {});
});</script>""",
    # The page rejects the file and clears the input.
    "upload_error": """<script>
document.getElementById('resume').addEventListener('change', (e) => {
  e.target.value = '';
  document.getElementById('resume-error').textContent = 'File type not supported';
});</script>""",
}

LONG_DESCRIPTION = (
    "<section class='description'><h2>About the role</h2>"
    + (
        "<p>You will build secure login flows with 2FA, OTP delivery and captcha "
        "integrations, maintain our two-factor authentication service and our authenticator "
        "app, and own the flows where users verify your identity. 'Please verify you are "
        "human' and 'Enter the verification code we sent' are strings our product shows.</p>"
    )
    * 6
    + "</section>"
)

AUTH_Q = "Are you legally authorized to work in India?"
# case -> (auth field, notice field)
DROPDOWN_CASES = {
    "actselect": (act_select("auth", AUTH_Q, ["Yes", "No"]), TEXT_NOTICE),
    "actselect_search": (
        NATIVE_AUTH.format(yes_selected=""),
        act_select(
            "notice",
            "Notice period",
            ["Immediate", "15 days", "30 days", "60 days"],
            searchable=True,
        ),
    ),
    "actselect_missing": (
        act_select("auth", AUTH_Q, ["Authorized", "Not authorized"]),
        TEXT_NOTICE,
    ),
    "actselect_ambiguous": (
        act_select("auth", AUTH_Q, ["Yes - citizen", "Yes - work permit", "No"]),
        TEXT_NOTICE,
    ),
    "actselect_selected": (act_select("auth", AUTH_Q, ["Yes", "No"], selected="Yes"), TEXT_NOTICE),
    "select_selected": (NATIVE_AUTH.format(yes_selected=" selected"), TEXT_NOTICE),
}

EXTRA = {
    "unknown": """<div class="field"><label for="night">Are you willing to work night shifts? *
      </label><input id="night" name="night" required></div>""",
    "sponsorship": """<div class="field"><label for="sp">Will you now or in the future require
      visa sponsorship? *</label><select id="sp" name="sp" required><option value="">Select
      </option><option>Yes</option><option>No</option></select></div>""",
    "us_auth": """<div class="field"><label for="us">Are you legally authorized to work in the
      United States? *</label><select id="us" name="us" required><option value="">Select
      </option><option>Yes</option><option>No</option></select></div>""",
    "consent": """<div class="field"><label><input type="checkbox" name="consent" required>
      I agree to the privacy policy *</label></div>""",
    "captcha": """<div class="g-recaptcha" data-sitekey="test-site-key"></div>""",
}
CHALLENGE_PAGES = {
    "otp": """<h1>Verify it's you</h1><p>Enter the verification code we sent to your email.</p>
      <input autocomplete="one-time-code" name="code"><button>Verify</button>""",
    "login": """<h1>Sign in to apply</h1><input type="email" name="u">
      <input type="password" name="p"><button>Sign in</button>""",
    "twofactor": """<h1>Two-factor authentication</h1>
      <p>Enter the code from your authenticator app.</p><input name="c">""",
    "identity": """<h1>Please verify your identity</h1><p>Upload a government ID.</p>""",
    "noform": """<h1>Backend Engineer</h1><p>A great job. Applications open soon.</p>""",
    "apply_button": """<h1>Backend Engineer</h1><p>Job description.</p>
      <a href="/jobs/normal">Apply now</a>""",
}


def page(title: str, body: str) -> bytes:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title></head>"
        f"<body>{body}</body></html>"
    ).encode()


FORM_CASES = {
    "normal",
    "ambiguous",
    "error",
    "duplicate",
    "validation",
    "description_words",
    *EXTRA,
    *DROPDOWN_CASES,
    *UPLOAD_JS,
}


def form_page(case: str) -> bytes:
    phone_attrs = 'pattern="[0-9]{10}"' if case == "validation" else ""
    auth, notice = DROPDOWN_CASES.get(case, (NATIVE_AUTH.format(yes_selected=""), TEXT_NOTICE))
    fields = BASE_FIELDS.format(
        phone_attrs=phone_attrs, auth_field=auth, notice_field=notice
    ) + EXTRA.get(case, "")
    description = LONG_DESCRIPTION if case == "description_words" else ""
    body = (
        f"<h1>Apply: Backend Engineer ({case})</h1>{description}"
        f'<form method="post" action="/submit/{case}" enctype="multipart/form-data">{fields}'
        '<button type="submit">Submit application</button></form>'
        + (ACT_SELECT_JS if case in DROPDOWN_CASES else "")
        + UPLOAD_JS.get(case, "")
    )
    return page(f"Apply {case}", body)


@dataclass
class ApplySite:
    host: str = "127.0.0.1"
    port: int = 0
    submissions: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[dict[str, Any]] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, case: str) -> str:
        return f"{self.base}/jobs/{case}"

    def start(self) -> "ApplySite":
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep test output quiet
                pass

            def _send(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                case = self.path.split("?")[0].removeprefix("/jobs/")
                if case in CHALLENGE_PAGES:
                    self._send(200, page(case, CHALLENGE_PAGES[case]))
                elif case in FORM_CASES:
                    self._send(200, form_page(case))
                else:
                    self._send(404, page("Not found", "<h1>404 Not found</h1>"))

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if self.path == "/upload":  # an async resume upload, not an application
                    site.uploads.append({"bytes": len(body)})
                    self._send(200, b"{}")
                    return
                case = self.path.removeprefix("/submit/")
                previous = [s for s in site.submissions if s["case"] == case]
                site.submissions.append(
                    {"case": case, "bytes": len(body), "has_pdf": b"%PDF" in body}
                )
                if case == "error":
                    self._send(500, page("Error", "<h1>Something went wrong</h1>"))
                elif case == "ambiguous":
                    self._send(200, page("Careers", "<h1>Careers</h1><p>See our open roles.</p>"))
                elif case == "duplicate" and previous:
                    self._send(200, page("Apply", "<p>You have already applied to this job.</p>"))
                else:
                    n = 10000 + len(site.submissions)
                    self._send(
                        200,
                        page(
                            "Thank you",
                            "<h1>Thank you for applying!</h1>"
                            f"<p>Your application ID is APP-{n}.</p>",
                        ),
                    )

            def do_PUT(self) -> None:
                self.do_POST()

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
