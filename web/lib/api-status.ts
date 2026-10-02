export type ApiStatus =
  | { state: "ready" }
  | { state: "not-ready" }
  | { state: "unreachable" };

const DEFAULT_API_URL = "http://127.0.0.1:4601";
const TIMEOUT_MS = 2000;

/**
 * Asks the API's readiness endpoint whether it can serve requests.
 * Never throws and never exposes the response body or the error.
 */
export async function getApiStatus(
  baseUrl: string = process.env.API_INTERNAL_URL ?? DEFAULT_API_URL,
): Promise<ApiStatus> {
  try {
    const response = await fetch(`${baseUrl}/readyz`, {
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    return response.ok ? { state: "ready" } : { state: "not-ready" };
  } catch {
    return { state: "unreachable" };
  }
}
