export type FailedJob = {
  job_id: string;
  title: string;
  company: string | null;
  url: string;
  error: string | null;
  stage: string | null;
  failed_at: string | null;
  screenshot: string | null;
  retries: number;
  application_id: string | null;
  session_id: string | null;
  screenshot_index: number | null;
};

export type FailedList = { items: FailedJob[]; total: number };

/** The failure screenshot, through the dashboard's backend proxy (or null if none). */
export function failedScreenshotUrl(f: FailedJob): string | null {
  if (!f.application_id || !f.session_id || f.screenshot_index === null) return null;
  return `/backend/api/applications/${encodeURIComponent(f.application_id)}/sessions/${encodeURIComponent(
    f.session_id,
  )}/screenshots/${f.screenshot_index}`;
}
