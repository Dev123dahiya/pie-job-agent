import { apiBaseUrl } from "@/lib/api";
import { isAllowedProxyPath } from "@/lib/proxy";

/**
 * Same-origin proxy so browser code can call the backend without CORS or exposing
 * PIE_API_URL. Resolved at request time (not baked in at build), and limited to the
 * PIE API paths the dashboard uses.
 */
async function forward(request: Request, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const joined = path.join("/");
  if (!isAllowedProxyPath(joined)) {
    return Response.json({ detail: "Not found" }, { status: 404 });
  }
  const search = new URL(request.url).search;
  const init: RequestInit = {
    method: request.method,
    headers: { "content-type": request.headers.get("content-type") ?? "application/json" },
    cache: "no-store",
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.text();
  }
  try {
    const res = await fetch(`${apiBaseUrl()}/${joined}${search}`, init);
    if (res.status === 204) return new Response(null, { status: 204 });
    // Pass bytes through untouched (resume PDFs are binary).
    const headers: Record<string, string> = {
      "content-type": res.headers.get("content-type") ?? "application/json",
    };
    const disposition = res.headers.get("content-disposition");
    if (disposition) headers["content-disposition"] = disposition;
    return new Response(await res.arrayBuffer(), { status: res.status, headers });
  } catch {
    return Response.json({ detail: "The backend is not reachable." }, { status: 502 });
  }
}

export const GET = forward;
export const POST = forward;
export const PUT = forward; // saved answers: edit
export const DELETE = forward; // saved answers: delete
