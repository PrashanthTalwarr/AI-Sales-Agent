import { NextResponse } from "next/server";
import { demoLeads, demoLastRun } from "@/lib/demo";

export async function GET() {
  return NextResponse.json({ leads: demoLeads(), last_run: demoLastRun });
}
