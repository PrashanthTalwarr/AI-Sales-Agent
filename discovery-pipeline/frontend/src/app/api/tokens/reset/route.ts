import { NextResponse } from "next/server";

export async function POST() {
  // Usage in the demo reflects the saved run, so there is nothing to reset.
  return NextResponse.json({ reset: true });
}
