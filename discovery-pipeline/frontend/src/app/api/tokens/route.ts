import { NextResponse } from "next/server";
import { demoTokenUsage } from "@/lib/demo";

export async function GET() {
  return NextResponse.json(demoTokenUsage);
}
