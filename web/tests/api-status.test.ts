import { afterEach, describe, expect, it, vi } from "vitest";
import { getApiStatus } from "@/lib/api-status";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getApiStatus", () => {
  it("reports ready when /readyz returns 200", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getApiStatus("http://api.test")).resolves.toEqual({ state: "ready" });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/readyz",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("reports not-ready when /readyz returns 503", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("db down", { status: 503 })));

    await expect(getApiStatus("http://api.test")).resolves.toEqual({ state: "not-ready" });
  });

  it("reports unreachable when fetch rejects", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("connect ECONNREFUSED")));

    await expect(getApiStatus("http://api.test")).resolves.toEqual({ state: "unreachable" });
  });
});
