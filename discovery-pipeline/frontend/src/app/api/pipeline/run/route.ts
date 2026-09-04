import { demoPipelineLog } from "@/lib/demo";

/**
 * Replays the log from a real pipeline run as SSE, in the same event shape the
 * FastAPI backend streams, so the UI needs no demo-specific handling.
 *
 * The demo cannot run the real pipeline: it takes minutes, needs three API keys,
 * and writes to disk. Replaying the actual output of a run that did happen is
 * honest about what it is while still showing the stages and their real numbers.
 */
export async function GET() {
  const encoder = new TextEncoder();
  const lines = demoPipelineLog;

  const stream = new ReadableStream({
    async start(controller) {
      const send = (obj: unknown) =>
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(obj)}\n\n`));

      send({ type: "log", text: "[DEMO] Replaying a saved pipeline run — no live API calls." });
      send({ type: "log", text: "" });

      for (const text of lines) {
        // Pace it so the stages are readable rather than dumped at once.
        // Section banners get a longer beat; plain lines go quickly.
        const delay = text.startsWith("====") ? 90 : text.trim() === "" ? 20 : 45;
        await new Promise((r) => setTimeout(r, delay));
        send({ type: "log", text });
      }

      send({ type: "done" });
      controller.close();
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
