/**
 * Proxy route for /coupons — forwards to the FastAPI backend server-side so
 * the browser never needs direct access to the Python backend (which lives on
 * 127.0.0.1:8000 in dev or behind a private tunnel in production).
 */

import { NextRequest, NextResponse } from "next/server";

const API_BASE =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const search = req.nextUrl.search; // includes leading "?" or is empty
  const url = `${API_BASE}/coupons${search}`;
  try {
    const res = await fetch(url, { cache: "no-store" });
    const body = await res.text();
    return new NextResponse(body, {
      status: res.status,
      headers: { "content-type": res.headers.get("content-type") ?? "application/json" },
    });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "upstream failure" },
      { status: 502 },
    );
  }
}
