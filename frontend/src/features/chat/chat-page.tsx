import { TierBadge } from "@/components/tier-badge";
import { NotWiredYet, PageHeader } from "@/components/page-shell";

/**
 * The main screen. In Phase 4 this streams agent events over SSE: a route
 * badge, a collapsible timeline of tool calls with timings, the answer arriving
 * token by token, and a citations drawer.
 */
export function ChatPage() {
  return (
    <>
      <PageHeader
        title="Chat"
        description="Ask about policy documents, client portfolios or back-office actions"
      />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <NotWiredYet
          phase="Phase 4 · Multi-agent graph"
          will={[
            "The route the supervisor chose, with its confidence score",
            "A timeline of tool calls as they run, each tagged with its risk tier",
            "The answer streaming in token by token over SSE",
            "A citations drawer opening the source document at the cited page",
          ]}
          needs="the LangGraph supervisor and the SSE event contract"
        />
        {/* The tier vocabulary is part of the shell, not of Phase 4: every
            screen that shows a tool call shows one of these three. */}
        <div className="mx-auto w-full max-w-xl px-4 pb-12">
          <p className="mb-2 text-xs text-muted-foreground">
            Tool calls will be labelled with the tier that decides whether a human must approve
            them:
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <TierBadge tier="READ" />
            <span className="text-xs text-muted-foreground">search_docs, run_sql</span>
            <TierBadge tier="LOW" className="ml-2" />
            <span className="text-xs text-muted-foreground">create_task</span>
            <TierBadge tier="HIGH" className="ml-2" />
            <span className="text-xs text-muted-foreground">rebalance_portfolio</span>
          </div>
        </div>
      </div>
    </>
  );
}
