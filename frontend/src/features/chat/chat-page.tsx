import { useCallback, useRef, useState } from "react";

import { PageHeader } from "@/components/page-shell";
import { TierBadge } from "@/components/tier-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ask, createThread, decideApproval } from "@/lib/stream";
import { applyEvent, newTurn, type Turn } from "./turn";

/**
 * The chat screen.
 *
 * Deliberately thin — Phase 4a's job is that the whole path exists, not that
 * it is pretty. What it shows: the route the supervisor chose, the answer, the
 * verified citations, and an inline approve/reject when the graph pauses on a
 * HIGH-risk action.
 *
 * The answer arrives as one event rather than token by token. The research
 * agent uses structured output, so the model streams JSON fragments and the
 * usable prose only exists once the object closes — Phase 7 parses that
 * incrementally. Nothing here fakes a reveal from the finished string.
 */

/** Until there is authentication, the decider is a placeholder. Phase 9. */
const DECIDED_BY = "adviser-7";

export function ChatPage() {
  const [message, setMessage] = useState("");
  const [turn, setTurn] = useState<Turn | null>(null);
  const [busy, setBusy] = useState(false);
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
      setTurn(newTurn(question));

      try {
        threadId.current ??= await createThread();
        for await (const streamed of ask({ threadId: threadId.current, message: question })) {
          setTurn((current) => (current ? applyEvent(current, streamed) : current));
        }
      } catch (error) {
        const text = error instanceof Error ? error.message : "the request failed";
        setTurn((current) => (current ? { ...current, error: text } : current));
      } finally {
        setBusy(false);
      }
    },
    [busy, message],
  );

  const decide = useCallback(async (decision: "approve" | "reject") => {
    setBusy(true);
    try {
      const approvalId = turn?.approval?.approvalId;
      if (!approvalId) return;
      await decideApproval(approvalId, decision, { decidedBy: DECIDED_BY });
      setTurn((current) => (current ? { ...current, decision, approval: undefined } : current));
    } catch (error) {
      const text = error instanceof Error ? error.message : "the decision failed";
      setTurn((current) => (current ? { ...current, error: text } : current));
    } finally {
      setBusy(false);
    }
  }, [turn]);

  return (
    <>
      <PageHeader
        title="Chat"
        description="Ask about policy documents, or request a back-office action"
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl space-y-4 px-4 pb-12">
          {turn ? <TurnView turn={turn} busy={busy} onDecide={decide} /> : <Empty />}
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
  onDecide: (decision: "approve" | "reject") => void;
}) {
  return (
    <div className="space-y-3 pt-4">
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
            <Button size="sm" disabled={busy} onClick={() => onDecide("approve")}>
              Approve
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide("reject")}>
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
