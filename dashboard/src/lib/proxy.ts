/** Backend paths the browser may reach through the /backend proxy. */
const ALLOWED = /^api\/(jobs|agent|health|dashboard|resumes|applications|logs|needs-input|runs|review)(\/[A-Za-z0-9_\-/]*)?$/;

export function isAllowedProxyPath(path: string): boolean {
  return ALLOWED.test(path) && !path.includes("..");
}
