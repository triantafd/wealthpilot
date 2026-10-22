import { cn } from "cn";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { ExecutionOutcome } from "@/components/execution-outcome";
import { PageHeader } from "@/components/page-shell";
import { TierBadge } from "@/components/tier-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Inline } from "@/lib/inline-markdown";
import { ask, createThread, decideApproval } from "@/lib/stream";
import type { ActionOutcome } from "@shared/stream-events";

import { useThreadTurns } from "./history";
import { applyEvent, newTurn, restore, type Turn, updateTurn } from "./turn";

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
 * **The thread id lives in the URL**, as `/chat/:threadId`. It used to be a
 * `useRef`, which meant a reload started a conversation the checkpointer knew
 * nothing about — the adviser's own history, their pending approvals and the
 * context behind "and for a junior account?" all silently gone. The id is now
 * the page's address, so a reload lands back on the same thread and a
 * follow-up continues it.
 *
 * **The transcript comes back with it.** A page that arrives with an id in its
 * URL fetches `GET /threads/{id}/turns` and maps each turn into the same
 * `Turn` the stream builds, so one component draws both and a restored
 * approval is decided by the same `decide()`. Only an id the page *arrived*
 * with is fetched: one it created itself has no history, and asking for it
 * would race the stream writing the turn.
 *
 * What is deliberately not used is `GET /threads/{id}`, which returns messages
 * as role and content only. Restoring from it would redraw the answers with
 * their citations, route and cost missing, and a reader cannot tell a citation
 * that was never there from one dropped on the way back.
 */

/** Until there is authentication, the decider is a placeholder. Phase 9. */
const DECIDED_BY = "adviser-7";

export function ChatPage() {
  const [message, setMessage] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement | null>(null);

  const { threadId: inUrl } = useParams<{ threadId: string }>();
  const navigate = useNavigate();
  // The thread this page is currently driving. The URL is the source of truth;
  // this is how the page tells its own navigation apart from someone else's.
  //
  // It has to, because React Router keeps this component mounted across
  // `/chat` -> `/chat/abc`, so "the id changed" cannot simply mean "start
  // again". Three cases, wanting different things: the id appearing because we
  // just created it (keep going — the first question is still streaming), the
  // id changing to another thread or the Back button (a different
  // conversation), and the id disappearing because the sidebar's Chat link was
  // clicked (a new one). Comparing against what we are driving separates them.
  const driving = useRef<string | null>(inUrl ?? null);

  // The thread whose history to restore. Initialised from the URL, because
  // this is the reload case and the effect below does not run on mount: the
  // page is already driving the id it was mounted with. A query keyed on the
  // id does run on mount, which is why the fetch hangs off this and not off
  // the effect.
  const [restoring, setRestoring] = useState<string | null>(inUrl ?? null);
  // The thread whose history has already been put on screen, so neither a
  // re-render nor a refetch can prepend it twice.
  const restored = useRef<string | null>(null);

  const address = inUrl ?? null;
  useEffect(() => {
    if (address === driving.current) return;
    driving.current = address;
    // A different conversation, so nothing on screen belongs to it.
    setTurns([]);
    setRestoring(address);
    // Nothing is restored any more, which matters for coming *back* to a
    // thread: its history is cached, so without this the guard below would
    // recognise it as already on screen and leave the transcript empty.
    restored.current = null;
  }, [address]);

  const history = useThreadTurns(restoring);
  useEffect(() => {
    const thread = history.data;
    if (!thread || thread.thread_id === restored.current) return;
    restored.current = thread.thread_id;
    // Prepended rather than assigned. Everything restored is older than
    // anything this page has streamed, by construction — so a question typed
    // while the fetch was still in flight survives it arriving.
    setTurns((current) => [...thread.turns.map(restore), ...current]);
  }, [history.data]);

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
        if (driving.current === null) {
          const started = await createThread();
          driving.current = started;
          // `replace`, not a push: the URL is gaining the identity of the
          // conversation already on screen rather than moving somewhere new,
          // and an empty `/chat` is not a place the Back button should return
          // to mid-answer.
          navigate(`/chat/${started}`, { replace: true });
        }
        for await (const streamed of ask({ threadId: driving.current, message: question })) {
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
    [busy, message, navigate],
  );

  const decide = useCallback(
    async (turnId: string, approvalId: string, decision: "approve" | "reject") => {
      // The turn is named by the caller rather than looked up as "the latest":
      // an approval belongs to the turn that raised it, and an adviser can
      // have asked something else since.
      setBusy(true);
      try {
        const decided = await decideApproval(approvalId, decision, { decidedBy: DECIDED_BY });
        setTurns((current) =>
          updateTurn(current, turnId, (turn) => ({
            ...turn,
            decision,
            approval: undefined,
            // What it actually did, rather than the assurance that it did
            // something. `null` is a fact too — see `ExecutionOutcome`.
            executed: decided.approval.execution,
            executionSpent: decided.approval.consumed_at !== null,
          })),
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
          {history.data?.truncated ? (
            <p className="pt-2 text-xs text-muted-foreground" data-testid="truncated">
              This conversation is long enough that its oldest turns are not shown.
            </p>
          ) : null}

          {turns.length > 0 ? (
            turns.map((item) => (
              <TurnView key={item.id} turn={item} busy={busy} onDecide={decide} />
            ))
          ) : history.isLoading ? (
            // `isLoading`, not `isPending`: a disabled query is pending
            // forever, so on `/chat` that would never show the invitation.
            <Restoring />
          ) : history.isError ? (
            <RestoreFailed message={history.error.message} />
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

/** While a reloaded page waits for its transcript.
 *
 *  Not the invitation below: a conversation that is about to reappear must not
 *  be announced as an empty one, however briefly. */
function Restoring() {
  return (
    <div className="pt-10 text-center text-sm text-muted-foreground" data-testid="restoring">
      <p>Restoring this conversation…</p>
    </div>
  );
}

/** When it cannot be restored.
 *
 *  Said out loud, because the alternative is an empty chat that reads as a
 *  conversation nobody ever had. The server's own message is kept: for a
 *  reload after the first turn failed, "no thread …" is the truth. */
function RestoreFailed({ message }: { message: string }) {
  return (
    <div className="pt-10 text-center text-sm" data-testid="restore-failed">
      <p className="text-destructive">This conversation could not be restored.</p>
      <p className="mt-1 text-xs text-muted-foreground">{message}</p>
      <p className="mt-3 text-xs text-muted-foreground">
        Anything asked from here starts the thread again with no earlier context.
      </p>
    </div>
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

/** Asking, as distinct from refusing.

 *  Both reach the screen as a paragraph, and they are opposites: one is a
 *  question a reply carries forward, the other is a dead end the adviser
 *  should stop pushing at. `propose` renders nothing — the approval card
 *  below, with its HIGH tier badge, says it louder than a label would.
 *
 *  Local to this file rather than a component beside `TierBadge`, which earns
 *  its place by appearing in the timeline, the inbox and the audit view. This
 *  appears once. */
function OutcomeNotice({ outcome }: { outcome?: ActionOutcome }) {
  if (outcome !== "ask_account" && outcome !== "refuse") return null;

  const asking = outcome === "ask_account";
  return (
    <p
      data-testid="outcome"
      title={
        asking
          ? "The agent needs one more detail. Reply and it will carry on."
          : "The agent will not propose this action."
      }
      className={cn(
        "inline-flex items-center rounded-sm px-1.5 py-0.5 text-[10px] font-medium",
        asking ? "bg-accent text-accent-foreground" : "bg-muted text-muted-foreground",
      )}
    >
      {asking ? "needs your answer" : "declined"}
    </p>
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

      <OutcomeNotice outcome={turn.outcome} />

      {turn.answer ? (
        <p className="text-sm leading-relaxed whitespace-pre-wrap" data-testid="answer">
          {/* The model emphasises figures: `**£1,500**`. `whitespace-pre-wrap`
              stays, so a newline still shows the day an answer has one. */}
          <Inline text={turn.answer} />
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
              {/* Quotes are verbatim from the same markdown corpus, so they
                  carry the same `**` a reader should not have to see. */}
              &ldquo;
              <Inline text={citation.quote} />
              &rdquo;
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
          {turn.decision === "approve" ? "Approved" : "Rejected"}
          {" — "}
          <ExecutionOutcome
            execution={turn.executed ?? null}
            status={turn.decision === "approve" ? "approved" : "rejected"}
            spent={turn.executionSpent ?? false}
          />
        </p>
      ) : null}

      {turn.error ? (
        <p className="text-sm text-destructive" data-testid="error">
          {turn.error}
        </p>
      ) : null}

      {turn.ms !== undefined ? (
        <p className="text-xs text-muted-foreground" data-testid="done">
          {turn.ms}ms
          {/* A restored turn can have a latency and no price: the model had
              none on file, and the `usage` table keeps an honest NULL for it.
              Hiding the whole line there would lose the latency as well. */}
          {turn.cost !== undefined ? ` · $${turn.cost.toFixed(6)}` : null}
        </p>
      ) : null}
    </div>
  );
}
