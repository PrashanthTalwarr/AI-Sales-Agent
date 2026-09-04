import { NextResponse } from "next/server";
import raw from "@/lib/demo-data.json";

export async function GET() {
  const data = raw as unknown as { outreach: Array<Record<string, unknown>> };
  return NextResponse.json({ results: data.outreach });
}
