import { demoChatReply } from "@/lib/demo";

/**
 * Streams a scripted chat reply token by token, matching the SSE shape the real
 * LangChain agent emits.
 *
 * The hosted demo has no Anthropic key, so rather than fake an LLM this answers
 * from the same saved run the rest of the demo reads — the numbers it quotes are
 * real. Questions outside what the agent's tools cover get an honest answer
 * saying so.
 */
export async function POST(req: Request) {
  const encoder = new TextEncoder();

  let message = "";
  try {
    const body = await req.json();
    message = typeof body?.message === "string" ? body.message : "";
  } catch {
    message = "";
  }

  const { text, tool } = demoChatReply(message);

  const stream = new ReadableStream({
    async start(controller) {
      const send = (obj: unknown) =>
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(obj)}\n\n`));

      // Brief pause so it reads as a response rather than an instant paste
      await new Promise((r) => setTimeout(r, 260));

      // Stream by word, keeping the trailing space so the text reflows correctly
      const parts = text.split(/(\s+)/);
      for (const part of parts) {
        if (part) {
          send({ type: "token", text: part });
          await new Promise((r) => setTimeout(r, 12));
        }
      }

      send({
        type: "done",
        tool_calls: tool ? [{ tool, input: { query: message } }] : [],
        refresh: false,
      });
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
