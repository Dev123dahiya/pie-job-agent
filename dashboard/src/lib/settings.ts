export type Limits = {
  total: number;
  naukri: number;
  linkedin: number;
  company_site: number;
  instahyre: number;
};

export type CurrentSettings = {
  keywords: string[];
  locations: string[];
  excluded_companies: string[];
  excluded_keywords: string[];
  freshness_hours: number;
  limits: Limits;
  min_match_score: number | null;
  answers: Record<string, string>;
  answer_questions: Record<string, string>;
  read_only: { models: { note: string; current: string }; experience_range: string; modes: string };
};

export type SettingsChange = Partial<{
  keywords: string[];
  locations: string[];
  excluded_companies: string[];
  excluded_keywords: string[];
  freshness_hours: number;
  limits: Limits;
  min_match_score: number;
  answers: Record<string, string>;
}>;

/** One entry per line; blank lines dropped. */
export function lines(text: string): string[] {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** Only what the user changed (the backend validates everything again). */
export function buildChange(current: CurrentSettings, form: {
  keywords: string;
  locations: string;
  excluded_companies: string;
  excluded_keywords: string;
  freshness_hours: number;
  limits: Limits;
  min_match_score: number;
  answers: Record<string, string>;
}): SettingsChange {
  const change: SettingsChange = {};
  const lists = ["keywords", "locations", "excluded_companies", "excluded_keywords"] as const;
  for (const key of lists) {
    const next = lines(form[key]);
    if (!same(next, current[key])) change[key] = next;
  }
  if (form.freshness_hours !== current.freshness_hours) change.freshness_hours = form.freshness_hours;
  if (!same(form.limits, current.limits)) change.limits = form.limits;
  if (form.min_match_score !== current.min_match_score) change.min_match_score = form.min_match_score;
  const answers: Record<string, string> = {};
  for (const [k, v] of Object.entries(form.answers)) {
    if (v.trim() && v.trim() !== (current.answers[k] ?? "")) answers[k] = v.trim();
  }
  if (Object.keys(answers).length) change.answers = answers;
  return change;
}
