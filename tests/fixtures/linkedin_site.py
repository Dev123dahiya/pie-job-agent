"""A local, fictional page shaped like LinkedIn's Easy Apply (checked 2026-10-04): a job page
with an "Easy Apply to this job" button; the form opens inside #interop-outlet's shadow root,
headed "Apply to <company>", with Next / Review / Submit application / Dismiss / Discard.
Runs only on 127.0.0.1 during tests; submissions are recorded in ``LinkedInSite.submissions``.

Cases (/jobs/view/<case>/):
  normal   contact (email prefilled, phone) -> questions (work authorization, notice period)
           -> review -> Submit application
  unknown  as normal, plus a required question PIE has no answer for
  stuck    Next never moves on
"""

import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PAGE = """<!doctype html><html><head><title>Job | LinkedIn</title></head><body>
<h1>Python Developer</h1><p>Acme · Gurugram</p>
<button aria-label="Easy Apply to this job" id="ea">Easy Apply</button>
<div id="interop-outlet"></div>
<script>
const CASE = "%CASE%";
const STEPS = [
  `<h3>Contact info</h3>
   <label for="email">Email address</label>
   <select id="email" required><option value="">Select an option</option>
     <option selected>candidate@example.com</option></select>
   <label for="phone">Mobile phone number</label><input id="phone" required>`,
  `<h3>Additional questions</h3>
   <fieldset><legend>Are you legally authorized to work in India?</legend>
     <label><input type="radio" name="auth" value="Yes" required>Yes</label>
     <label><input type="radio" name="auth" value="No">No</label></fieldset>
   <label for="notice">Notice period</label><input id="notice" required>` +
   (CASE === "unknown"
     ? `<label for="k8s">How many years of Kubernetes experience do you have?</label>
        <input id="k8s" required>` : ""),
  `<h3>Review your application</h3><p>Check everything before you submit.</p>`,
];
let step = 0;
const host = document.getElementById("interop-outlet");
const root = host.attachShadow({ mode: "open" });
function render() {
  const last = step === STEPS.length - 1;
  const label = step === STEPS.length - 2 ? "Review" : "Next";
  const next = last ? `<button id="submit">Submit application</button>`
    : `<button id="next">${label}</button>`;
  root.innerHTML = `<div class="panel"><h2>Apply to Acme</h2>
    <button aria-label="Dismiss" id="dismiss">x</button>
    <form onsubmit="return false">${STEPS[step]}</form>
    ${step > 0 ? '<button id="back">Back</button>' : ''} ${next}</div>`;
  const n = root.getElementById("next");
  if (n) n.onclick = () => { if (CASE !== "stuck") { step++; render(); } };
  const b = root.getElementById("back");
  if (b) b.onclick = () => { step--; render(); };
  const s = root.getElementById("submit");
  if (s) s.onclick = async () => {
    await fetch("/submit/" + CASE, { method: "POST", body: "{}" });
    root.innerHTML = `<div class="panel"><h2>Apply to Acme</h2>
      <p>Your application was sent to Acme</p></div>`;
  };
  root.getElementById("dismiss").onclick = () => {
    root.innerHTML = `<div class="panel"><h2>Discard application?</h2>
      <button id="discard">Discard</button></div>`;
    root.getElementById("discard").onclick = async () => {
      await fetch("/discard/" + CASE, { method: "POST", body: "{}" });
      root.innerHTML = "";
    };
  };
}
document.getElementById("ea").onclick = () => { step = 0; render(); };
</script></body></html>"""


@dataclass
class LinkedInSite:
    port: int = 0
    submissions: list[str] = field(default_factory=list)
    discards: list[str] = field(default_factory=list)
    _server: Any = None

    def url(self, case: str) -> str:
        return f"http://127.0.0.1:{self.port}/jobs/view/{case}/"

    def start(self) -> "LinkedInSite":
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                case = self.path.strip("/").split("/")[-1]
                body = PAGE.replace("%CASE%", case).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                kind, case = self.path.strip("/").split("/")[:2]
                (site.submissions if kind == "submit" else site.discards).append(case)
                self.send_response(200)
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
