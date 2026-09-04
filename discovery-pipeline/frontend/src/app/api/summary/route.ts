import { NextResponse } from "next/server";
import { demoSummary } from "@/lib/demo";

export async function GET() {
  return NextResponse.json(demoSummary());
}
