export type PausedQuestion = {
  question_key: string;
  question: string;
  kind: "standard" | "form" | "sensitive" | "challenge";
  reason: string | null;
  answerable: boolean;
  /** The form's own dropdown choices; the answer must be one of them. */
  options?: string[];
};

export type PausedJob = {
  job_id: string;
  application_id: string | null;
  title: string;
  company: string | null;
  url: string;
  reason: string | null;
  paused_at: string | null;
  in_browser: boolean;
  questions: PausedQuestion[];
};

export type SavedAnswer = {
  id: string;
  question_key: string;
  question: string;
  answer: string;
  source: string;
  updated_at: string;
};

export type NeedsInput = { jobs: PausedJob[]; saved_answers: SavedAnswer[] };

export const KIND_LABEL: Record<PausedQuestion["kind"], string> = {
  standard: "Standard answer",
  form: "Question on the form",
  sensitive: "Sensitive: only you can answer",
  challenge: "Act in the browser",
};

/** Answers worth sending: answerable questions with a non-empty answer. */
export function answersToSend(questions: PausedQuestion[], values: Record<string, string>) {
  return questions
    .filter((q) => q.answerable && (values[q.question_key] ?? "").trim() !== "")
    .map((q) => ({ question_key: q.question_key, question: q.question, answer: values[q.question_key].trim() }));
}
