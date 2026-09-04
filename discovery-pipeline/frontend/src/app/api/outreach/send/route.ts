import { NextResponse } from "next/server";

export async function POST() {
  // Deliberately does not fake a send. The demo has no Resend key and should not
  // imply an email went out.
  return NextResponse.json(
    {
      detail:
        "Email sending is disabled on the hosted demo. Running locally, this delivers " +
        "to your configured test inbox and records the send so nobody is emailed twice.",
    },
    { status: 501 }
  );
}
