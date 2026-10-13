import { useCallback, useEffect, useRef, useState } from "react";

import { PageHeader } from "@/components/page-shell";
import { TierBadge } from "@/components/tier-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ask, createThread, decideApproval } from "@/lib/stream";
import { applyEvent, newTurn, type Turn, updateTurn } from "./turn";

/**
 * The chat screen.
 *
 * Deliberately thin — Phase 4a's job is that the whole path exists, not that
 * it is pretty. What it shows: the route the supervisor chose, the answer, the
 * verified citations, and an inline approve/reject when the graph pauses on a
 * HIGH-risk action.
 *
 * The answer arrives in pieces, and nothing here fakes a reveal from a
 * finished string. The research agent uses structured output, so the model
 * streams JSON fragments; the backend grows the answer out of them with a
 * partial parser and sends each piece as a `token` event. `applyEvent` appends
 * them, and replaces the lot with the finished text when the turn ends — see
 * `turn.ts`.
 *
 * The conversation is kept in this component and not reloaded from the server.
 * The checkpointer does persist every turn, but `GET /threads/{id}` returns
 * messages as role and content only — no citations, no per-turn route, no cost
 * — so restoring from it would redraw the questions and answers with the
 * evidence missing, which is worse than an honest empty page. Surviving a
 * refresh is its own roadmap item, and needs that endpoint to return the whole
 * turn first.
 */

/** Until there is authentication, the decider is a placeholder. Phase 9. */
const DECIDED_BY = "adviser-7";

export function ChatPage() {
  const [message, setMessage] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement | null>(null);
  // One thread for the page's life, so a follow-up continues the conversation
  // rather than starting a new one the checkpointer knows nothing about.
  const threadId = useRef<string | null>(null);

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      const question = message.trim();
      if (!question || busy) return;

      setBusy(true);
      setMessage("");
      const asked = newTurn(question);
      setTurns((current) => [...current, asked]);

      try {
        threadId.current ??= await createThread();
        for await (const streamed of ask({ threadId: threadId.current, message: question })) {
          setTurns((current) =>
            updateTurn(current, asked.id, (turn) => applyEvent(turn, streamed)),
          );
        }
      } catch (error) {
        const text = error instanceof Error ? error.message : "the request failed";
        setTurns((current) => updateTurn(current, asked.id, (turn) => ({ ...turn, error: text })));
      } finally {
        setBusy(false);
      }
    },
    [busy, message],
  );

  const decide = useCallback(
    async (turnId: string, approvalId: string, decision: "approve" | "reject") => {
      // The turn is named by the caller rather than looked up as "the latest":
      // an approval belongs to the turn that raised it, and an adviser can
      // have asked something else since.
      setBusy(true);
      try {
        await decideApproval(approvalId, decision, { decidedBy: DECIDED_BY });
        setTurns((current) =>
          updateTurn(current, turnId, (turn) => ({ ...turn, decision, approval: undefined })),
        );
      } catch (error) {
        const text = error instanceof Error ? error.message : "the decision failed";
        setTurns((current) => updateTurn(current, turnId, (turn) => ({ ...turn, error: text })));
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  // Follow the newest turn. A chat that leaves the answer below the fold reads
  // as broken; `turns.length` rather than `turns` so this fires when a turn is
  // added, not on every streamed event.
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns.length]);

  return (
    <>
      <PageHeader
        title="Chat"
        description="Ask about policy documents, or request a back-office action"
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl space-y-4 px-4 pb-12">
          {turns.length > 0 ? (
            turns.map((item) => (
              <TurnView key={item.id} turn={item} busy={busy} onDecide={decide} />
            ))
          ) : (
            <Empty />
          )}
          <div ref={bottom} />
        </div>
      </div>

      <form onSubmit={submit} className="border-t bg-background px-4 py-3">
        <div className="mx-auto flex w-full max-w-2xl gap-2">
          <Input
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="Is there a floor on what we charge?"
            aria-label="Message"
            disabled={busy}
          />
          <Button type="submit" disabled={busy || !message.trim()}>
            {busy ? "Working…" : "Ask"}
          </Button>
        </div>
      </form>
    </>
  );
}

function Empty() {
  return (
    <div className="pt-10 text-center text-sm text-muted-foreground">
      <p>Ask a question about the firm&apos;s policies, or request a rebalance.</p>
      <p className="mt-1 text-xs">
        A request that changes an account pauses for approval before anything happens.
      </p>
    </div>
  );
}

function TurnView({
  turn,
  busy,
  onDecide,
}: {
  turn: Turn;
  busy: boolean;
  onDecide: (turnId: string, approvalId: string, decision: "approve" | "reject") => void;
}) {
  const approvalId = turn.approval?.approvalId;
  return (
    <div className="space-y-3 border-t pt-4 first:border-t-0" data-testid="turn">
      <p className="text-sm font-medium">{turn.question}</p>

      {turn.route ? (
        <p className="text-xs text-muted-foreground" data-testid="route">
          routed to <span className="font-medium text-foreground">{turn.route.route}</span>
          {" · "}
          confidence {turn.route.confidence.toFixed(2)}
          {turn.retrievedCount ? ` · ${turn.retrievedCount} passages searched` : null}
        </p>
      ) : null}

      {turn.answer ? (
        <p className="text-sm leading-relaxed whitespace-pre-wrap" data-testid="answer">
          {turn.answer}
        </p>
      ) : null}

      {turn.citations.length > 0 ? (
        <ul className="space-y-1" data-testid="citations">
          {turn.citations.map((citation) => (
            <li
              key={`${citation.documentId}-${citation.page}-${citation.quote}`}
              className="border-l-2 pl-3 text-xs text-muted-foreground"
            >
              <span className="font-medium text-foreground">
                {citation.documentId} · page {citation.page}
              </span>
              <br />
              &ldquo;{citation.quote}&rdquo;
            </li>
          ))}
        </ul>
      ) : null}

      {turn.approval ? (
        <div className="rounded-md border p-3" data-testid="approval">
          <div className="flex items-center gap-2">
            <TierBadge tier={turn.approval.action.tier} />
            <span className="text-xs font-medium">{turn.approval.action.tool}</span>
          </div>
          <p className="mt-2 text-sm">{turn.approval.action.summary}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Nothing has happened yet. This runs only if you approve it.
          </p>
          <div className="mt-3 flex gap-2">
            <Button
              size="sm"
              disabled={busy}
              onClick={() => approvalId && onDecide(turn.id, approvalId, "approve")}
            >
              Approve
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => approvalId && onDecide(turn.id, approvalId, "reject")}
            >
              Reject
            </Button>
          </div>
        </div>
      ) : null}

      {turn.decision ? (
        <p className="text-xs text-muted-foreground" data-testid="decision">
          {turn.decision === "approve" ? "Approved — the action has run." : "Rejected — nothing ran."}
        </p>
      ) : null}

      {turn.error ? (
        <p className="text-sm text-destructive" data-testid="error">
          {turn.error}
        </p>
      ) : null}

      {turn.cost !== undefined ? (
        <p className="text-xs text-muted-foreground" data-testid="done">
          {turn.ms}ms · ${turn.cost.toFixed(6)}
        </p>
      ) : null}
    </div>
  );
}
