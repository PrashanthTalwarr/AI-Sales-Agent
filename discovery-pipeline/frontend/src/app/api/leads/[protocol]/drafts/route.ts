import { NextResponse } from "next/server";
import { demoDraftsFor } from "@/lib/demo";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ protocol: string }> }
) {
  const { protocol } = await params;
  const name = decodeURIComponent(protocol);
  const drafts = demoDraftsFor(name);
  if (drafts.length === 0) {
    return NextResponse.json(
      {
        detail:
          `No drafts for '${name}'. This demo carries drafts for the three leads that ` +
          `qualified in the saved run — open Rocket Pool, Lido, or EigenCloud.`,
      },
      { status: 404 }
    );
  }
  return NextResponse.json({ protocol: name, drafts });
}
