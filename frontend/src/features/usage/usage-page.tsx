import { NotWiredYet, PageHeader } from "@/components/page-shell";

/** Cost, tokens and latency per request. Phase 2. */
export function UsagePage() {
  return (
    <>
      <PageHeader title="Usage" description="What the assistant costs and how fast it answers" />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <NotWiredYet
          phase="Phase 2 · Observability and cost"
          will={[
            "Cost per day, in dollars, from the usage table",
            "Tokens broken down by agent, so an expensive route is visible",
            "p50 and p95 latency, not just an average",
          ]}
          needs="Langfuse tracing and the /usage endpoint"
        />
      </div>
    </>
  );
}
