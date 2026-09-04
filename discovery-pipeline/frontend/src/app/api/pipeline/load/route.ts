import { NextResponse } from "next/server";
import { demoSummary } from "@/lib/demo";

export async function POST() {
  const s = demoSummary();
  return NextResponse.json({ loaded: true, ...s });
}
